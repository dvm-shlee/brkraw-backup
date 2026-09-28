"""Archive verification levels (0.2.0).

- ``list``: file names and sizes in the archive match the raw folder (needs raw).
- ``crc``: every member of the archive is read and checked against its stored
  CRC-32 (no raw needed). Catches damaged zip data.
- ``content``: ``crc`` plus a CRC-32 of every raw file compared with the CRC the
  archive stores for it (needs raw). Catches same-size content changes.

Failure reasons are fixed strings so tests and the registry can rely on them:
``unreadable_zip``, ``duplicate_member``, ``crc_mismatch``, ``bad_data``,
``missing_files``, ``extra_files``, ``size_mismatch``, ``content_mismatch``,
``raw_missing``, ``raw_changed``.

Nothing here writes to the archive or to the raw folder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _dt
import os
from pathlib import Path
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Tuple

from ._manifest import compare_manifests, file_crc32

LEVELS = ("list", "crc", "content")
_EXAMPLES = 20


@dataclass
class VerifyResult:
    ok: bool
    level: str
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""

    @property
    def reason(self) -> Optional[str]:
        return ",".join(self.reasons) if self.reasons else None

    def as_record(self, archive_path: Path) -> Dict[str, Any]:
        return {
            "level": self.level,
            "ok": self.ok,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "archive_path": str(archive_path),
        }


def _now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def zip_root_prefix(names: List[str]) -> str:
    """The single top folder all members share, or '' if there is none."""
    clean = [n.strip("/") for n in names if n.strip("/")]
    if not clean:
        return ""
    first = clean[0].split("/")[0]
    for n in clean:
        if n != first and not n.startswith(first + "/"):
            return ""
    # a lone file at the top is not a folder prefix
    if all(n == first for n in clean):
        return ""
    return first


def _archive_key(archive_path: Path) -> Optional[str]:
    name = archive_path.name
    if name.endswith(".partial"):
        name = name[: -len(".partial")]
    lower = name.lower()
    for suffix in (".zip", ".pvdatasets"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return None


def _choose_prefix(names: List[str], archive_path: Path, raw_path: Optional[Path]) -> str:
    """Top folder to strip from member names before comparing with raw.

    brkraw-backup writes members as `<key>/...`, so a common top folder equal to the
    archive's key is stripped. Another common top folder is stripped only when the
    raw folder has no folder of that name (an archive made without the wrapper).
    """
    prefix = zip_root_prefix(names)
    if not prefix:
        return ""
    if prefix == _archive_key(archive_path):
        return prefix
    if raw_path is not None and (Path(raw_path) / prefix).is_dir():
        return ""
    return prefix


def _archive_members(
    zf: zipfile.ZipFile, archive_path: Path, raw_path: Optional[Path]
) -> Tuple[Dict[str, zipfile.ZipInfo], List[str]]:
    """Map relative name -> ZipInfo (top folder stripped); also return duplicated names."""
    infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    prefix = _choose_prefix([i.filename for i in infos], Path(archive_path), raw_path)
    members: Dict[str, zipfile.ZipInfo] = {}
    dupes: List[str] = []
    for info in infos:
        name = info.filename.strip("/")
        if prefix and name.startswith(prefix + "/"):
            name = name[len(prefix) + 1:]
        if not name:
            continue
        if name in members:
            dupes.append(name)
            continue
        members[name] = info
    return members, sorted(set(dupes))


def _raw_stats(raw_path: Path) -> Dict[str, Tuple[int, ...]]:
    """relative name -> (size, mtime_ns, ctime_ns, inode) for every file under raw_path.

    ctime cannot be set back by a program (utime changes it too), so a same-size
    edit that restores the modification time is still seen as a change.
    """
    out: Dict[str, Tuple[int, ...]] = {}

    def _walk_error(exc: OSError) -> None:
        raise exc

    for dirpath, _dirs, filenames in os.walk(raw_path, onerror=_walk_error):
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(raw_path).as_posix()
            st = full.stat()
            out[rel] = (int(st.st_size), int(st.st_mtime_ns), int(st.st_ctime_ns), int(st.st_ino))
    return out


def _read_members(zf: zipfile.ZipFile, members: Dict[str, zipfile.ZipInfo]) -> List[Tuple[str, str]]:
    """Read every member fully; return [(name, reason)] for members that fail."""
    bad: List[Tuple[str, str]] = []
    for name in sorted(members):
        info = members[name]
        try:
            with zf.open(info, "r") as fh:
                while fh.read(1 << 20):
                    pass
        except zipfile.BadZipFile as exc:
            bad.append((name, "crc_mismatch" if "CRC" in str(exc) else "bad_data"))
        except zlib.error:
            bad.append((name, "bad_data"))
        except (EOFError, OSError, NotImplementedError, RuntimeError, ValueError):
            bad.append((name, "bad_data"))
    return bad


def verify_archive(
    archive_path: Path,
    raw_path: Optional[Path] = None,
    *,
    level: str = "crc",
) -> VerifyResult:
    if level not in LEVELS:
        raise ValueError("unknown verify level: %r" % (level,))
    res = VerifyResult(ok=False, level=level, checked_at=_now())
    needs_raw = level in ("list", "content")
    if needs_raw and (raw_path is None or not Path(raw_path).is_dir()):
        res.reasons.append("raw_missing")
        return res

    try:
        zf = zipfile.ZipFile(archive_path, "r")
    except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
        res.reasons.append("unreadable_zip")
        res.details["error"] = type(exc).__name__
        return res

    with zf:
        members, dupes = _archive_members(zf, Path(archive_path), raw_path if needs_raw else None)
        res.details["archive_files"] = len(members)
        if dupes:
            res.reasons.append("duplicate_member")
            res.details["duplicate_members"] = dupes[:_EXAMPLES]

        if level in ("crc", "content"):
            bad = _read_members(zf, members)
            for kind in ("crc_mismatch", "bad_data"):
                names = [n for n, r in bad if r == kind]
                if names:
                    res.reasons.append(kind)
                    res.details[kind] = names[:_EXAMPLES]

        if needs_raw:
            assert raw_path is not None
            raw_path = Path(raw_path)
            with_crc = level == "content"
            raw_manifest: Dict[str, Tuple[int, Optional[int]]] = {}
            try:
                before = _raw_stats(raw_path)
                for rel, st in before.items():
                    crc = file_crc32(raw_path / rel) if with_crc else None
                    raw_manifest[rel] = (st[0], crc)
            except OSError as exc:  # raw vanished or became unreadable during the check
                res.reasons.append("raw_changed")
                res.details["raw_changed"] = ["raw folder could not be read: %s" % type(exc).__name__]
                res.ok = False
                return res
            arc_manifest: Dict[str, Tuple[int, Optional[int]]] = {
                name: (int(info.file_size), int(info.CRC) if with_crc else None)
                for name, info in members.items()
            }
            cmp = compare_manifests(raw_manifest, arc_manifest)
            res.details["raw_files"] = len(raw_manifest)
            for key, reason in (
                ("missing", "missing_files"),
                ("extra", "extra_files"),
                ("size_mismatch", "size_mismatch"),
                ("crc_mismatch", "content_mismatch"),
            ):
                if cmp[key]:
                    res.reasons.append(reason)
                    res.details[reason] = cmp[key][:_EXAMPLES]
            try:
                after = _raw_stats(raw_path)
            except OSError:
                after = {}
            if after != before:
                changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
                res.reasons.append("raw_changed")
                res.details["raw_changed"] = changed[:_EXAMPLES]

    res.ok = not res.reasons
    return res
