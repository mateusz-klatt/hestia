"""Opaque private capsules; constructors and read paths never provision storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from uuid import UUID, uuid4

from .config import BudgetConfigError

MAX_FILE_BYTES = 1024 * 1024
KINDS = ("code", "candidate", "session")


class ConsentFilesError(BudgetConfigError):
    code = "consent_files"

    def __init__(self):
        super().__init__("Budget consent file operation failed")


def _reference(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError
    return value


def _payload(kind, value):
    if kind not in KINDS or not isinstance(value, dict):
        raise ValueError
    if kind == "session":
        if set(value) != {"session_id"}:
            raise ValueError
        _reference(value["session_id"])
    if kind == "code":
        code = value.get("code")
        if (set(value) != {"code"} or not isinstance(code, str) or not 1 <= len(code) <= 8192
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in code)):
            raise ValueError
    return value


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError


def _unlink(directory, name):
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


class ConsentFiles:
    def __init__(self, directory):
        try:
            self.directory = Path(directory).absolute()
        except (OSError, ValueError, TypeError):
            raise ConsentFilesError() from None

    def _directory(self, *, create=False):
        if create:
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError
        except (OSError, ValueError):
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _file(directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or info.st_size > MAX_FILE_BYTES):
                raise ValueError
        except (OSError, ValueError):
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _present(directory, name):
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _locate(self, directory, ref, kind):
        _reference(ref)
        if kind is not None and kind not in KINDS:
            raise ValueError
        claim = self._file(directory, ref + ".claim")
        try:
            if os.fstat(claim).st_size:
                raise ValueError
        finally:
            os.close(claim)
        found = [item for item in KINDS if self._present(directory, ref + "." + item + ".json")]
        if len(found) != 1 or kind is not None and found[0] != kind:
            raise ValueError
        name = ref + "." + found[0] + ".json"
        return name, found[0]

    def _allocate(self, directory):
        for _ in range(4):
            ref = str(uuid4())
            try:
                claim = os.open(ref + ".claim", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=directory)
            except FileExistsError:
                continue
            try:
                os.fsync(claim)
            except OSError:
                _unlink(directory, ref + ".claim")
                raise
            finally:
                os.close(claim)
            if any(self._present(directory, ref + "." + kind + ".json") for kind in KINDS):
                _unlink(directory, ref + ".claim")
                continue
            return ref
        raise ValueError

    @staticmethod
    def _atomic(directory, ref, kind, data):
        name = "." + ref + "." + secrets.token_hex(16) + ".tmp"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(name, ref + "." + kind + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            _unlink(directory, name)

    def put(self, kind, payload):
        directory, ref = None, None
        try:
            data = json.dumps(_payload(kind, payload), sort_keys=True, ensure_ascii=False,
                              allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                raise ValueError
            directory = self._directory(create=True)
            ref = self._allocate(directory)
            self._atomic(directory, ref, kind, data)
            return ref
        except (OSError, ValueError, TypeError, RecursionError):
            if directory is not None and ref is not None:
                try:
                    for name in (ref + "." + kind + ".json", ref + ".claim"):
                        _unlink(directory, name)
                except OSError:
                    pass  # An unreferenced failed capsule is never adopted or served as fallback.
            raise ConsentFilesError() from None
        finally:
            if directory is not None:
                os.close(directory)

    def path(self, ref, *, kind=None):
        try:
            directory = self._directory()
            try:
                name, _ = self._locate(directory, ref, kind)
                descriptor = self._file(directory, name)
                os.close(descriptor)
                return str(self.directory / name)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError):
            raise ConsentFilesError() from None

    def get(self, ref, kind):
        try:
            directory = self._directory()
            try:
                name, _ = self._locate(directory, ref, kind)
                descriptor = self._file(directory, name)
                with os.fdopen(descriptor, "rb") as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError
                value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant)
                return _payload(kind, value)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError, RecursionError):
            raise ConsentFilesError() from None

    def delete(self, ref):
        try:
            _reference(ref)
            try:
                directory = self._directory()
            except FileNotFoundError:
                return
            try:
                for name in [ref + "." + kind + ".json" for kind in KINDS] + [ref + ".claim"]:
                    _unlink(directory, name)
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError, TypeError):
            raise ConsentFilesError() from None
