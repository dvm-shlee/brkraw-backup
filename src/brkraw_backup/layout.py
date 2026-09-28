"""Where brkraw-backup keeps things under the archive folder, and how names map to paths.

    <archive_root>/<key>.zip                     archive made by `create`/`repair`
    <archive_root>/<key>.PvDatasets              ParaVision export (also counted as an archive)
    <archive_root>/<key>.zip.partial             archive being written (never read as an archive)
    <archive_root>/.brkraw-backup-journal/<key>.json   unfinished repair/remove
    <archive_root>/.brkraw-backup-trash/<key>/<UTC>/   old archives kept by repair/remove

Read-only helpers only; nothing here changes files except the journal writers.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

JOURNAL_DIR = ".brkraw-backup-journal"
TRASH_DIR = ".brkraw-backup-trash"
PARTIAL_SUFFIX = ".partial"
REPAIR_STEPS = ("started", "partial_written", "partial_verified", "trash_copied", "replaced", "registry_updated")
REMOVE_STEPS = ("started", "moved", "registry_updated")


def utcnow() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def create_new(path: Path, mode: str = "wb"):
    """Open a fresh file at `path` for writing, never writing through an existing entry.

    An existing entry of that name (a stale temp file, or a symbolic or hard link
    someone left there) is unlinked first, which removes only that name; then the
    file is created with O_EXCL, which also refuses to follow a symbolic link.
    """
    if os.path.lexists(str(path)):
        os.unlink(str(path))
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")


def check_file_name(name: str) -> Optional[str]:
    """Problem text if `name` is not a plain file name (used for --registry)."""
    if not isinstance(name, str) or not name.strip() or name in (".", ".."):
        return "empty name"
    if "/" in name or "\\" in name or os.sep in name or "\x00" in name:
        return "must be a file name in the archive folder, not a path"
    return None


def roots_problem(raw_root: Optional[Path], archive_root: Path) -> Optional[str]:
    """Refuse an archive folder that is the raw folder or inside it."""
    if raw_root is None:
        return None
    raw = raw_root.resolve(strict=False)
    arc = archive_root.resolve(strict=False)
    if arc == raw or raw in arc.parents:
        return "the archive folder may not be the raw folder or inside it (%s)" % arc
    return None


def check_key(key: str) -> Optional[str]:
    """Return a problem text if `key` is not a plain dataset name, else None."""
    if not isinstance(key, str) or not key.strip():
        return "empty name"
    if key != key.strip():
        return "name has leading or trailing spaces"
    if key in (".", "..") or key.startswith("."):
        return "name may not start with '.'"
    if "/" in key or "\\" in key or os.sep in key or "\x00" in key:
        return "name may not contain a path separator"
    return None


def is_candidate_raw_dir(name: str) -> bool:
    if not name or name.startswith("."):
        return False
    if "import" in name:
        return False
    return True


def raw_candidates(raw_root: Path) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not raw_root.is_dir():
        return out
    for entry in sorted(raw_root.iterdir()):
        if entry.is_dir() and is_candidate_raw_dir(entry.name):
            out[entry.name] = entry
    return out


def archive_key(name: str) -> Optional[str]:
    lower = name.lower()
    if lower.endswith(".zip"):
        key = name[:-4]
    elif lower.endswith(".pvdatasets"):
        key = name[: -len(".pvdatasets")]
    elif name.endswith("PvDatasets"):
        key = name[: -len("PvDatasets")]
        if key.endswith("."):
            key = key[:-1]
    else:
        return None
    key = key.strip()
    if not key or key.startswith("."):
        return None
    return key


def archive_candidates(archive_root: Path) -> Dict[str, List[Path]]:
    """key -> every archive file for that key (more than one means duplicate_archive)."""
    out: Dict[str, List[Path]] = {}
    if not archive_root.is_dir():
        return out
    for entry in sorted(archive_root.iterdir()):
        if not entry.is_file():
            continue
        key = archive_key(entry.name)
        if key is None:
            continue
        out.setdefault(key, []).append(entry)
    return out


def partial_path(archive_root: Path, key: str) -> Path:
    return archive_root / ("%s.zip%s" % (key, PARTIAL_SUFFIX))


def partial_files(archive_root: Path) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not archive_root.is_dir():
        return out
    suffix = ".zip" + PARTIAL_SUFFIX
    for entry in sorted(archive_root.iterdir()):
        if entry.name.endswith(suffix) and not entry.name.startswith("."):
            out[entry.name[: -len(suffix)]] = entry
    return out


# --- journal -----------------------------------------------------------------

def journal_dir(archive_root: Path) -> Path:
    return archive_root / JOURNAL_DIR


def journal_path(archive_root: Path, key: str) -> Path:
    return journal_dir(archive_root) / ("%s.json" % key)


def read_journal(archive_root: Path, key: str) -> Optional[Dict[str, Any]]:
    path = journal_path(archive_root, key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"op": "unknown", "key": key, "step": "unreadable"}
    if not isinstance(data, dict):
        return {"op": "unknown", "key": key, "step": "unreadable"}
    return data


def journal_problem(archive_root: Path, key: str, journal: Dict[str, Any]) -> Optional[str]:
    """Problem text if a journal does not describe this key's own files, else None.

    A resumed run uses the paths in the journal, so every path must be exactly
    the one brkraw-backup would have written for this key.
    """
    op = journal.get("op")
    steps = {"repair": REPAIR_STEPS, "remove": REMOVE_STEPS}.get(str(op))
    if steps is None:
        return "journal has an unknown operation (%r)" % (op,)
    if journal.get("key") != key:
        return "journal is for another key (%r)" % (journal.get("key"),)
    if journal.get("step") not in steps:
        return "journal has an unknown step (%r)" % (journal.get("step"),)
    arc = journal.get("archive_path")
    if not isinstance(arc, str):
        return "journal has no archive path"
    arc_p = Path(arc)
    if arc_p.parent.resolve(strict=False) != archive_root.resolve(strict=False) or archive_key(arc_p.name) != key:
        return "journal archive path is not this key's archive in the archive folder"
    if op == "repair":
        part = journal.get("partial_path")
        if not isinstance(part, str) or Path(part).resolve(strict=False) != partial_path(archive_root, key).resolve(strict=False):
            return "journal partial path is not this key's partial file"
    trash = journal.get("trash_path")
    if trash is None:
        if op == "remove" or steps.index(str(journal.get("step"))) >= steps.index("trash_copied"):
            return "journal has no trash path"
    else:
        t = Path(str(trash))
        if t.name != arc_p.name or t.parent.parent.resolve(strict=False) != trash_key_dir(archive_root, key).resolve(strict=False):
            return "journal trash path is not inside this key's trash folder"
    return None


def list_journals(archive_root: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    jdir = journal_dir(archive_root)
    if not jdir.is_dir():
        return out
    for entry in sorted(jdir.iterdir()):
        if entry.name.endswith(".json") and not entry.name.startswith("."):
            key = entry.name[: -len(".json")]
            j = read_journal(archive_root, key)
            if j is not None:
                out[key] = j
    return out


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_journal(archive_root: Path, journal: Dict[str, Any]) -> None:
    jdir = journal_dir(archive_root)
    if jdir.is_symlink():
        raise OSError("journal folder is a symbolic link: %s" % jdir)
    jdir.mkdir(exist_ok=True)
    path = journal_path(archive_root, str(journal["key"]))
    tmp = path.with_name(path.name + ".tmp")
    journal = dict(journal)
    journal["updated_at"] = utcnow()
    with create_new(tmp, "w") as fh:
        json.dump(journal, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(jdir)


def drop_journal(archive_root: Path, key: str) -> None:
    path = journal_path(archive_root, key)
    if path.exists():
        path.unlink()
        _fsync_dir(path.parent)


# --- trash -------------------------------------------------------------------

def trash_root(archive_root: Path) -> Path:
    return archive_root / TRASH_DIR


def trash_key_dir(archive_root: Path, key: str) -> Path:
    return trash_root(archive_root) / key


def trash_link_problem(archive_root: Path, key: str) -> Optional[str]:
    for p in (trash_root(archive_root), trash_key_dir(archive_root, key)):
        if p.is_symlink():
            return "trash folder is a symbolic link: %s" % p.name
        if p.exists() and not p.is_dir():
            return "trash path is not a folder: %s" % p.name
    return None


def trash_generations(archive_root: Path, key: str) -> List[Path]:
    kdir = trash_key_dir(archive_root, key)
    if not kdir.is_dir() or kdir.is_symlink():
        return []
    return sorted(p for p in kdir.iterdir() if not p.name.startswith("."))


def new_generation(archive_root: Path, key: str) -> Path:
    """Create and return a new, empty trash generation folder <trash>/<key>/<UTC stamp>/."""
    problem = trash_link_problem(archive_root, key)
    if problem:
        raise OSError(problem)
    kdir = trash_key_dir(archive_root, key)
    kdir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    n = 0
    while True:
        gen = kdir / (stamp if n == 0 else "%s-%d" % (stamp, n))
        try:
            gen.mkdir()
            return gen
        except FileExistsError:
            n += 1


def fsync_dir(path: Path) -> None:
    _fsync_dir(path)
