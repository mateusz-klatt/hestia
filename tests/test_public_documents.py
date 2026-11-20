"""Public legal HTML stays accessible without opening other paths or methods."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from hestia import auth, store_sql, web


class PublicDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dist = Path(temporary.name)
        self.index = self.dist / "index.html"
        self.index.write_text("<!doctype html><title>Hestia</title>", encoding="utf-8")
        self.documents = {
            "/privacy-policy.html": b'<!doctype html><html lang="en"><title>Privacy Policy</title></html>',
            "/terms-of-service.html": b'<!doctype html><html lang="en"><title>Terms of Service</title></html>',
        }
        for path, content in self.documents.items():
            (self.dist / path[1:]).write_bytes(content)
        (self.dist / "private.json").write_text('{"private":true}', encoding="utf-8")
        patches = [mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(self.dist)}),
                   mock.patch.object(web, "_AUTH_ENABLED", True),
                   mock.patch.object(web, "_SESSION_SECRET", b"public-document-test"),
                   mock.patch.object(store_sql, "current_user_role", return_value="admin")]
        for patch in patches:
            result = patch.start()
            self.addCleanup(patch.stop)
        self.role_lookup = result
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def signed_headers(self):
        token = auth.make_session("operator", now=web._now(), secret=b"public-document-test")
        return {"Authorization": "Bearer " + token}

    async def test_get_and_head_are_public_html_with_file_metadata(self):
        for path, expected in self.documents.items():
            with self.subTest(path=path):
                response = await self.client.get(path, headers={"Authorization": "Bearer invalid.token"})
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.read(), expected)
                self.assertTrue(response.headers["Content-Type"].startswith("text/html"))
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(int(response.headers["Content-Length"]), len(expected))
                self.assertIn("ETag", response.headers)
                self.assertIn("Last-Modified", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                head = await self.client.head(path)
                self.assertEqual(head.status, 200)
                self.assertEqual(await head.read(), b"")
                for name in ("Content-Type", "Content-Length", "ETag", "Last-Modified", "X-Content-Type-Options"):
                    self.assertEqual(head.headers[name], response.headers[name])
                queried = await self.client.get(path + "?path=private.json")
                self.assertEqual(await queried.read(), expected)
        self.role_lookup.assert_not_called()

    async def test_other_methods_and_paths_keep_authentication_and_role_gates(self):
        for path in self.documents:
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                with self.subTest(method=method, path=path):
                    response = await self.client.request(method, path)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(await response.read(), b"")
                    signed = await self.client.request(method, path, headers=self.signed_headers())
                    self.assertEqual(signed.status, 405)
                    self.assertEqual(await signed.read(), b"")
                    self.assertEqual({item.strip() for item in signed.headers["Allow"].split(",")}, {"GET", "HEAD"})
        for path in ("/privacy-policy", "/privacy-policy.html/", "/terms-of-service.html/other",
                     "/public/privacy-policy.html", "/private.json", "/index.html", "/api/discovery"):
            for method in ("GET", "HEAD"):
                with self.subTest(method=method, path=path):
                    response = await self.client.request(method, path)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(await response.read(), b"")
        for path in ("/", "/ui/", "/assets/app.js"):
            self.assertEqual((await self.client.head(path)).status, 401)
        self.role_lookup.return_value = "viewer"
        denied = await self.client.post("/privacy-policy.html", headers=self.signed_headers())
        self.assertEqual(denied.status, 403)
        self.assertEqual(await denied.read(), b"")

    async def test_arbitrary_dist_files_are_not_exposed_to_authenticated_users(self):
        for path in ("/private.json", "/public/privacy-policy.html", "/terms-of-service.html/other"):
            for method in ("GET", "HEAD"):
                response = await self.client.request(method, path, headers=self.signed_headers())
                self.assertEqual(response.status, 404)
                self.assertEqual(await response.read(), b"")

    async def test_missing_build_or_document_is_public_empty_404(self):
        async def assert_missing(path):
            for method in ("GET", "HEAD"):
                response = await self.client.request(method, path)
                self.assertEqual(response.status, 404)
                self.assertEqual(await response.read(), b"")
                self.assertEqual(response.headers["Content-Length"], "0")
        with mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(self.dist / "absent")}):
            for path in self.documents:
                await assert_missing(path)
        self.index.unlink()
        for path in self.documents:
            await assert_missing(path)
        self.index.write_text("<!doctype html><title>Hestia</title>", encoding="utf-8")
        for path in self.documents:
            target = self.dist / path[1:]
            target.unlink()
            await assert_missing(path)
            target.mkdir()  # A directory at the document path is not a built HTML file.
            await assert_missing(path)
