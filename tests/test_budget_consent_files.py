"""Private capsule boundary, using synthetic secret strings and temporary files."""
import io
from contextlib import contextmanager
import json
import math
import os
import stat
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from hestia.budget import consent_files as module
from hestia.budget.config import credentials
from tests.test_budget_store import APP, configuration

SESSION = "00000000-0000-4000-8000-000000000023"
SECRET = "SYNTHETIC_SECRET_NEVER_LOG"


class ConsentFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "private"
        self.files = module.ConsentFiles(self.directory)

    def reject(self, operation):
        with self.assertRaises(module.ConsentFilesError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "consent_files")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_constructor_and_missing_read_delete_never_provision_directory(self):
        ref = SESSION
        self.assertFalse(self.directory.exists())
        self.reject(lambda: self.files.path(ref))
        self.reject(lambda: self.files.get(ref, "code"))
        self.files.delete(ref)
        self.assertFalse(self.directory.exists())
        self.reject(lambda: module.ConsentFiles(None))

    def test_roundtrip_kind_metadata_permissions_and_legacy_session_compatibility(self):
        payloads = {"session": {"session_id": SESSION}, "code": {"code": SECRET},
                    "candidate": {"label": "Żółw", "accounts": [], "authorized": True}}
        refs = []
        for kind, payload in payloads.items():
            ref = self.files.put(kind, payload)
            refs.append(ref)
            self.assertEqual(str(UUID(ref)), ref)
            self.assertEqual(self.files.get(ref, kind), payload)
            filename = Path(self.files.path(ref))
            self.assertTrue(filename.is_absolute())
            self.assertEqual(filename.stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.directory / (ref + ".claim")).stat().st_size, 0)
            self.assertEqual(self.files.path(ref, kind=kind), str(filename))
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        filename = Path(self.files.path(refs[0]))
        filename.chmod(0o400)
        self.assertEqual(self.files.get(refs[0], "session"), {"session_id": SESSION})
        key = self.root / "key.pem"; key.write_bytes(b"SYNTHETICKEY"); key.chmod(0o600)
        config = configuration()
        from dataclasses import replace
        self.assertEqual(credentials(replace(config, key_file=str(key)),
                                     replace(config.connections[0], session_file=str(filename))), (b"SYNTHETICKEY", SESSION))
        self.files.delete(refs[0]); self.files.delete(refs[0])
        self.reject(lambda: self.files.path(refs[0]))
        self.assertEqual(len(set(refs)), 3)

    def test_payload_schema_bounds_and_json_values_fail_without_creating_storage(self):
        cases = [("bad", {}), ("candidate", []), ("session", {}), ("session", {"session_id": APP, "extra": SECRET}),
                 ("session", {"session_id": None}), ("session", {"session_id": "not-a-uuid"}),
                 ("code", {}), ("code", {"code": ""}), ("code", {"code": None}),
                 ("code", {"code": "x" * 8193}), ("code", {"code": "bad code"}),
                 ("code", {"code": "bad\x00code"}), ("code", {"code": "bad\x7fcode"}),
                 ("candidate", {"bad": math.nan}), ("candidate", {"bad": object()}),
                 ("candidate", {"bad": "\ud800"})]
        cycle = {}; cycle["self"] = cycle
        cases.append(("candidate", cycle))
        for kind, payload in cases:
            with self.subTest(kind=kind):
                self.reject(lambda: self.files.put(kind, payload))
        with mock.patch.object(module, "MAX_FILE_BYTES", 10):
            self.reject(lambda: self.files.put("candidate", {"value": SECRET}))
        self.assertFalse(self.directory.exists())

    def test_invalid_or_wrong_kind_refs_do_not_traverse_or_disclose(self):
        ref = self.files.put("code", {"code": SECRET})
        for bad in (None, 123, "../" + SECRET, "00000000-0000-4000-8000-ABCDEFABCDEF", ref.replace("-", "")):
            self.reject(lambda: self.files.path(bad))
            self.reject(lambda: self.files.delete(bad))
        self.reject(lambda: self.files.path(ref, kind="unknown"))
        self.reject(lambda: self.files.get(ref, "session"))
        (self.directory / (ref + ".candidate.json")).write_text("{}")
        self.reject(lambda: self.files.path(ref))

    def test_existing_capsule_metadata_and_read_never_write_or_fsync(self):
        ref = self.files.put("session", {"session_id": SESSION})
        with mock.patch.object(module.os, "fsync", side_effect=AssertionError("Read must not fsync")), \
                mock.patch.object(module.os, "unlink", side_effect=AssertionError("Read must not delete")), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("Read must not create")):
            self.assertTrue(self.files.path(ref, kind="session").endswith(".session.json"))
            self.assertEqual(self.files.get(ref, "session"), {"session_id": SESSION})

    def test_directory_permissions_ownership_and_symlink_fail_closed(self):
        ref = self.files.put("code", {"code": SECRET})
        self.directory.chmod(0o755)
        for operation in (lambda: self.files.put("candidate", {}), lambda: self.files.path(ref), lambda: self.files.delete(ref)):
            self.reject(operation)
        self.directory.chmod(0o700)
        with mock.patch.object(module.os, "geteuid", return_value=os.geteuid() + 1):
            self.reject(lambda: self.files.get(ref, "code"))
        alias = self.root / "alias"; alias.symlink_to(self.directory, target_is_directory=True)
        other = module.ConsentFiles(alias)
        self.reject(lambda: other.put("candidate", {}))
        self.assertEqual(self.files.get(ref, "code"), {"code": SECRET})

    def test_payload_file_type_permissions_owner_and_size_are_checked_without_following_symlinks(self):
        ref = self.files.put("code", {"code": SECRET})
        path = Path(self.files.path(ref))
        path.chmod(0o644)
        self.reject(lambda: self.files.path(ref))
        path.chmod(0o600)
        original = module.os.fstat
        def foreign(fd):
            item = original(fd)
            return SimpleNamespace(st_mode=item.st_mode,
                                   st_uid=item.st_uid + (stat.S_ISREG(item.st_mode) and item.st_size > 0), st_size=item.st_size)
        with mock.patch.object(module.os, "fstat", side_effect=foreign):
            self.reject(lambda: self.files.get(ref, "code"))
        with mock.patch.object(module, "MAX_FILE_BYTES", 2):
            self.reject(lambda: self.files.get(ref, "code"))
        outside = self.root / "outside"; outside.write_text(SECRET)
        path.unlink(); path.symlink_to(outside)
        self.reject(lambda: self.files.get(ref, "code"))
        self.files.delete(ref)
        self.assertEqual(outside.read_text(), SECRET)
        ref = self.files.put("code", {"code": SECRET}); path = Path(self.files.path(ref))
        path.unlink(); os.mkfifo(path, 0o600)
        self.reject(lambda: self.files.get(ref, "code"))

    def test_claim_and_corrupt_json_cannot_be_served(self):
        ref = self.files.put("candidate", {"test": True})
        claim = self.directory / (ref + ".claim")
        claim.write_text("not-empty")
        self.reject(lambda: self.files.path(ref))
        claim.write_text("")
        path = Path(self.files.path(ref))
        for content in (b"\xff", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b"{"):
            path.write_bytes(content)
            self.reject(lambda: self.files.get(ref, "candidate"))
        path.write_text('{}')
        fdopen = module.os.fdopen
        @contextmanager
        def grew(fd, mode):
            with fdopen(fd, mode), io.BytesIO(b"x" * (module.MAX_FILE_BYTES + 1)) as result:
                yield result
        with mock.patch.object(module.os, "fdopen", side_effect=grew):
            self.reject(lambda: self.files.get(ref, "candidate"))

    def test_uuid_namespace_never_overwrites_an_existing_or_orphaned_capsule(self):
        first = self.files.put("candidate", {"original": True})
        orphan = "00000000-0000-4000-8000-000000000024"
        orphan_path = self.directory / (orphan + ".code.json")
        orphan_path.write_text("untouched")
        final = "00000000-0000-4000-8000-000000000025"
        with mock.patch.object(module, "uuid4", side_effect=[UUID(first), UUID(orphan), UUID(final)]):
            self.assertEqual(self.files.put("candidate", {}), final)
        self.assertEqual(self.files.get(first, "candidate"), {"original": True})
        self.assertEqual(orphan_path.read_text(), "untouched")
        with mock.patch.object(module, "uuid4", return_value=UUID(first)):
            self.reject(lambda: self.files.put("candidate", {}))

    def test_atomic_write_failures_are_safe_and_leave_no_published_reference(self):
        with mock.patch.object(module.os, "fsync", side_effect=OSError(SECRET)):
            self.reject(lambda: self.files.put("code", {"code": SECRET}))
        self.assertEqual(list(self.directory.iterdir()), [])
        with mock.patch.object(module.os, "replace", side_effect=OSError(SECRET)):
            self.reject(lambda: self.files.put("code", {"code": SECRET}))
        self.assertEqual(list(self.directory.iterdir()), [])
        with mock.patch.object(module.os, "replace", side_effect=OSError(SECRET)), \
                mock.patch.object(module, "_unlink", side_effect=PermissionError(SECRET)):
            self.reject(lambda: self.files.put("candidate", {}))
        with mock.patch.object(module.os, "fsync", side_effect=OSError(SECRET)):
            self.reject(lambda: self.files.delete(SESSION))
