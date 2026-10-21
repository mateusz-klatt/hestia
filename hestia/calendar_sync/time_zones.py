"""Canonical names from declared IANA Links in the selected timezone data source.

Never infer equivalence from offsets or sampled dates. System and packaged tzdata
can intentionally disagree (notably when one includes backzone history), so a
system TZif file must never borrow another database's alias declarations.
"""
from functools import lru_cache
from importlib import resources
from pathlib import Path
import zoneinfo


def _parse_links(text: str) -> dict[str, str]:
    links = {}
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) == 3 and fields[0] in ("L", "Link"):
            links[fields[2]] = fields[1]
    return links


@lru_cache(maxsize=16)
def _file_links(path: str) -> dict[str, str]:
    try:
        return _parse_links(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}


@lru_cache(maxsize=1)
def _package_links() -> dict[str, str]:
    try:
        return _parse_links(resources.files("tzdata.zoneinfo").joinpath("tzdata.zi").read_text(encoding="utf-8"))
    except (ImportError, OSError, UnicodeError):
        return {}


def _resolve(key: str, links: dict[str, str], exists) -> str:
    original, seen = key, set()
    while key in links:
        if key in seen:
            return original
        seen.add(key)
        key = links[key]
    # Preserve a valid source key if its manifest is partial or inconsistent.
    resolved = key if exists(key) else original
    # Retain Hestia's established spelling for the intrinsic UTC zone.
    return "UTC" if resolved == "Etc/UTC" else resolved


def _system_source(key: str) -> Path | None:
    for directory in zoneinfo.TZPATH:
        root = Path(directory)
        if root.joinpath(key).is_file():
            return root
    return None


def canonical_zone(key: str) -> str:
    """Resolve a validated ZoneInfo key using its actual source's Link chains.

    Missing manifests, unavailable packages, cycles and unmapped keys retain the
    supplied name. Like ZoneInfo itself, manifests are cached for the process;
    restart after changing the installed tzdata or timezone search path.
    """
    root = _system_source(key)
    if root is not None:
        return _resolve(key, _file_links(str(root / "tzdata.zi")), lambda name: _system_source(name) == root)
    # ZoneInfo falls back to the first-party package only when no TZPATH file
    # supplies this key. A package manifest must not override system semantics.
    try:
        root = resources.files("tzdata.zoneinfo")
        if not root.joinpath(*key.split("/")).is_file():
            return key
        return _resolve(key, _package_links(), lambda name: _system_source(name) is None and root.joinpath(*name.split("/")).is_file())
    except (ImportError, OSError):
        return key
