"""Decode live Hestia responses with the pinned Vesta 0.9.0 OpenAPI contract."""

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from hestia import api_contract, auth, db, proxy, store_sql, web
from hestia.budget import device_token
from tests import test_budget_api as budget_fixture
from tests import test_budget_device_tokens as device_fixture
from tests.test_budget_receipts import confirm_values, receipt_text

VESTA_REVISION = "ddf1e1dc200b147b9ac9ef7597c40f9441036581"
VESTA_OPENAPI = Path(__file__).parent / "fixtures" / "vesta-0.9.0-openapi.json"


class PinnedDecoder:
    """Small test-only OpenAPI reader for the response types used by Vesta 0.9.0."""

    def __init__(self):
        self.document = json.loads(VESTA_OPENAPI.read_text(encoding="utf-8"))
        self.assert_revision_contract()

    def assert_revision_contract(self):
        assert self.document["info"]["version"] == "0.9.0"
        assert VESTA_REVISION == "ddf1e1dc200b147b9ac9ef7597c40f9441036581"

    def response(self, method, path, status, payload):
        operation = self.document["paths"][path][method.lower()]
        schema = operation["responses"][str(status)]["content"]["application/json"]["schema"]
        return self.decode(schema, payload)

    def decode(self, schema, value):
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return self.decode(self.document["components"]["schemas"][name], value)
        for part in schema.get("allOf", []):
            self.decode(part, value)
        if "oneOf" in schema:
            matches = sum(self._accepts(part, value) for part in schema["oneOf"])
            assert matches == 1, (schema, value)
        if "anyOf" in schema:
            assert any(self._accepts(part, value) for part in schema["anyOf"]), (schema, value)
        if "const" in schema:
            assert value == schema["const"]
        if "enum" in schema:
            assert value in schema["enum"]
        kinds = schema.get("type")
        if kinds is not None:
            kinds = [kinds] if isinstance(kinds, str) else kinds
            assert any(self._type_matches(kind, value) for kind in kinds), (kinds, value)
        if isinstance(value, list) and "items" in schema:
            for item in value:
                self.decode(schema["items"], item)
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            for required in schema.get("required", []):
                assert required in value, (required, value)
            for key, item in value.items():
                if item is None and key not in schema.get("required", []):
                    continue
                if key in properties:
                    self.decode(properties[key], item)
                elif schema.get("additionalProperties") is False:
                    raise AssertionError((key, value))
                elif isinstance(schema.get("additionalProperties"), dict):
                    self.decode(schema["additionalProperties"], item)
        return value

    def _accepts(self, schema, value):
        try:
            self.decode(schema, value)
            return True
        except (AssertionError, KeyError):
            return False

    @staticmethod
    def _type_matches(kind, value):
        return {
            "object": lambda: isinstance(value, dict),
            "array": lambda: isinstance(value, list),
            "string": lambda: isinstance(value, str),
            "boolean": lambda: type(value) is bool,
            "integer": lambda: type(value) is int,
            "number": lambda: type(value) in (int, float),
            "null": lambda: value is None,
        }[kind]()


class Vesta090HTTPTests(unittest.IsolatedAsyncioTestCase):
    rows = budget_fixture.BudgetAPITests.rows
    token = budget_fixture.BudgetAPITests.token
    headers = budget_fixture.BudgetAPITests.headers
    assert_private_headers = budget_fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {
            device_token.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
            "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(device_token.reset_limits)
        await budget_fixture.BudgetAPITests.asyncSetUp(self)
        key = device_fixture._make_rule_key(self.path.parent)
        key_patch = mock.patch.dict(os.environ, {device_fixture.RULE_KEY_FILE: str(key)})
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.password = "synthetic-vesta-password"
        self.password_hash = auth.hash_password(self.password)
        with self.sessions.begin() as session:
            for username in ("alice", "bob", "operator", "viewer"):
                session.get(db.User, username).password_hash = self.password_hash
        self.assertEqual(store_sql.activate_catalog_compat(path=self.path), "activated")
        from types import SimpleNamespace
        self.client.server.app[web._RT_KEY] = SimpleNamespace(
            state=proxy.State(), registry=SimpleNamespace(mode="proxy"), mode="proxy",
        )
        self.decoder = PinnedDecoder()

    async def _post(self, path, body, headers=None):
        return await self.client.post(
            path, data=json.dumps(body),
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    async def _decoded(self, method, path, status, *, body=None, headers=None):
        outgoing = dict(headers or {})
        kwargs = {}
        if body is not None:
            outgoing["Content-Type"] = "application/json"
            kwargs["data"] = json.dumps(body)
        response = await self.client.request(method, path, headers=outgoing, **kwargs)
        self.assertEqual(response.status, status, await response.text())
        payload = await response.json()
        self.decoder.response(method, path, status, payload)
        return payload

    async def test_vesta_login_identity_discovery_control_and_users_decode(self):
        with mock.patch.object(web, "_audit"):
            login = await self._decoded("POST", "/api/login", 200, body={
                "user": "operator", "password": self.password, "bearer": True,
            })
        self.assertEqual(set(login), {"ok", "user", "token"})
        cached = login["token"]
        with mock.patch.object(web, "_merged_discovery", return_value={}):
            await self._decoded("GET", "/api/discovery", 200, headers={"Authorization": f"Bearer {cached}"})
        with mock.patch.object(web, "_dispatch_op", return_value=web._json(200, {"ok": True, "sent": "01"})):
            controlled = await self._decoded("POST", "/api/control", 200, body={
                "op": "switch", "node": 5, "on": True,
            }, headers={"Authorization": f"Bearer {cached}"})
            self.assertTrue(controlled["ok"])
            denied = await self.client.post("/api/control", json={
                "op": "switch", "node": 5, "on": True,
            }, headers={"Authorization": f"Bearer {self.token('viewer')}"})
            self.assertEqual(denied.status, 403)

        self.client.session.cookie_jar.clear()
        for username, role in (("viewer", "viewer"), ("operator", "operator"), ("alice", "admin")):
            whoami = await self._decoded("GET", "/api/whoami", 200, headers=self.headers(username))
            self.assertEqual(whoami, {"user": username, "role": role})
            self.assertEqual(set(whoami), {"user", "role"})
        users = await self._decoded("GET", "/api/users", 200, headers=self.headers("alice"))
        self.assertTrue(users["users"])
        self.assertTrue(all(row["role"] in ("viewer", "operator", "admin") for row in users["users"]))

    async def test_both_pairing_paths_receipts_and_self_revoke_decode(self):
        pair_response = await self._post(
            device_token.PAIR_PATH, {}, self.headers("alice", origin=True),
        )
        self.assertEqual(pair_response.status, 200)
        paired = await pair_response.json()
        api_contract.BudgetReceiptDevicePairResult.model_validate(paired)
        redeemed = await self._decoded("POST", device_token.REDEEM_PATH, 200, body={
            "code": paired["code"], "device_name": "Vesta QR 0.9.0",
        })
        self.assertEqual(set(redeemed), {"token", "token_id", "scope", "expires_at"})

        password = await self._decoded("POST", device_token.LOGIN_PATH, 200, body={
            "username": "alice", "password": self.password, "device_name": "Vesta Password 0.9.0",
        })
        self.assertEqual(len(password["token"]), 43)
        auth_header = {"Authorization": f"Bearer {password['token']}"}
        parsed = await self._decoded("POST", device_token.PARSE_PATH, 200, body={
            "text": receipt_text(),
        }, headers=auth_header)
        confirmed = await self._decoded("POST", device_token.CONFIRM_PATH, 200, body=confirm_values(parsed),
                                       headers=auth_header)
        await self._decoded("POST", device_token.DELETE_PATH, 200, body={
            "receipt_id": confirmed["receipt"]["receipt_id"],
            "expected_version": confirmed["receipt"]["version"],
        }, headers=auth_header)
        await self._decoded("POST", device_token.REVOKE_PATH, 200, body={
            "token_id": redeemed["token_id"],
        }, headers={"Authorization": f"Bearer {redeemed['token']}"})
        rejected = await self._decoded("POST", device_token.PARSE_PATH, 401, body={
            "text": receipt_text(),
        }, headers={"Authorization": f"Bearer {redeemed['token']}"})
        self.assertEqual(rejected["error"], "device_token_revoked")


if __name__ == "__main__":
    unittest.main()
