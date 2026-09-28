"""Commands that change files (0.2.0): create, repair, remove, purge.

Safety rules (BRK-0053 (5)):
- Nothing here deletes or changes a raw folder. Raw is only read.
- `create` never touches an existing archive. It writes `<key>.zip.partial`,
  verifies it at `content` level and only then puts it in place (no overwrite).
- `repair KEY` keeps a journal, writes and verifies `<key>.zip.partial`, puts a
  copy of the old archive in the trash (hard link, or copy + byte compare), and
  only then swaps the new file in with `os.replace`. The archive path always holds
  a whole archive. It refuses without raw.
- `remove KEY` only moves the archive into the trash (one `os.replace`).
- `purge KEY` only deletes trash generations, after confirmation.
- Every KEY of a call is checked before anything changes; there is no
  "all archives" default for repair/remove/purge.

`_step_hook` lets tests stop a run right after any journal step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import filecmp
import os
from pathlib import Path
import shutil
import zipfile
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import layout
from .verify import VerifyResult, verify_archive

REPAIR_STEPS = layout.REPAIR_STEPS
REMOVE_STEPS = layout.REMOVE_STEPS

# Test hook: called as _step_hook(op, key, step) after each journal step is written.
_step_hook: Optional[Callable[[str, str, str], None]] = None

Recorder = Callable[[str, Dict[str, Any]], None]
ProgressReporter = Callable[[int, int, str], None]


class TargetError(Exception):
    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class OperationError(Exception):
    """A step failed in a way that leaves the journal for a later run."""


@dataclass
class Target:
    key: str
    raw_path: Optional[Path] = None
    archive_path: Optional[Path] = None
    journal: Optional[Dict[str, Any]] = None
    skip: Optional[str] = None
    generations: List[Path] = field(default_factory=list)


@dataclass
class OpResult:
    key: str
    ok: bool
    message: str
    verify: Optional[VerifyResult] = None
    archive_path: Optional[Path] = None
    trash_path: Optional[Path] = None
    deleted: List[Path] = field(default_factory=list)


# --- target checks -----------------------------------------------------------

def _unique(keys: Sequence[str]) -> List[str]:
    out: List[str] = []
    for k in keys:
        if k not in out:
            out.append(k)
    return out


def _archive_problem(archive_root: Path, path: Path) -> Optional[str]:
    if path.is_symlink():
        return "archive is a symbolic link (%s)" % path.name
    try:
        parent = path.resolve(strict=True).parent
    except OSError:
        return "archive not found (%s)" % path.name
    if parent != archive_root.resolve(strict=False):
        return "archive is outside the archive folder (%s)" % path.name
    return None


def resolve_targets(
    op: str,
    keys: Sequence[str],
    *,
    raw_root: Optional[Path],
    archive_root: Path,
    path: Optional[str] = None,
) -> List[Target]:
    """Check every KEY for `op` before anything changes. Raise TargetError listing all problems."""
    problems: List[str] = []
    keys = _unique(list(keys))
    if not keys:
        raise TargetError(["name at least one KEY (there is no all-archives default for %s)" % op])
    if path is not None and len(keys) != 1:
        problems.append("--path needs exactly one KEY")
    raws = layout.raw_candidates(raw_root) if raw_root is not None else {}
    arcs = layout.archive_candidates(archive_root)
    targets: List[Target] = []

    for key in keys:
        bad = layout.check_key(key)
        if bad:
            problems.append("%s: %s" % (key, bad))
            continue
        t = Target(key=key, raw_path=raws.get(key))
        journal = layout.read_journal(archive_root, key)
        if journal is not None:
            jp = layout.journal_problem(archive_root, key, journal)
            if jp:
                problems.append(
                    "%s: %s; nothing was changed. Check %s by hand"
                    % (key, jp, layout.journal_path(archive_root, key))
                )
                continue
            if op == "purge" or journal.get("op") != op:
                problems.append(
                    "%s: unfinished %s (step %s); run `brkraw backup %s %s` first"
                    % (key, journal.get("op"), journal.get("step"), journal.get("op"), key)
                )
                continue
            t.journal = journal

        cands = list(arcs.get(key, []))
        if path is not None:
            chosen = Path(path).expanduser()
            if not chosen.is_absolute():
                chosen = archive_root / chosen
            match = [c for c in cands if c.resolve(strict=False) == chosen.resolve(strict=False)]
            if not match:
                problems.append("%s: --path is not an archive of this key: %s" % (key, path))
                continue
            cands = match
        if len(cands) > 1 and op != "purge":
            problems.append(
                "%s: duplicate_archive (%s); pass --path to choose one"
                % (key, ", ".join(c.name for c in cands))
            )
            continue
        if cands:
            t.archive_path = cands[0]
            if op in ("repair", "remove"):
                ap = _archive_problem(archive_root, t.archive_path)
                if ap:
                    problems.append("%s: %s" % (key, ap))
                    continue

        if op in ("repair", "remove", "purge"):
            tp = layout.trash_link_problem(archive_root, key)
            if tp:
                problems.append("%s: %s" % (key, tp))
                continue

        if op == "create":
            if t.raw_path is None:
                problems.append("%s: no raw folder with this name" % key)
                continue
            if t.archive_path is not None:
                t.skip = "archive exists (%s)" % t.archive_path.name
        elif op == "repair":
            if t.raw_path is None:
                problems.append("%s: raw folder missing; repair needs raw (nothing changed)" % key)
                continue
            if t.archive_path is None and t.journal is None:
                problems.append("%s: no archive to repair; use `brkraw backup create %s`" % (key, key))
                continue
            if t.journal is not None:
                t.archive_path = Path(str(t.journal.get("archive_path")))
        elif op == "remove":
            if t.journal is not None:
                t.archive_path = Path(str(t.journal.get("archive_path")))
            elif t.archive_path is None:
                problems.append("%s: no archive with this name" % key)
                continue
        elif op == "purge":
            gens = layout.trash_generations(archive_root, key)
            links = [g.name for g in gens if g.is_symlink() or not g.is_dir()]
            if links:
                problems.append("%s: trash entries that are not plain folders: %s" % (key, ", ".join(links)))
                continue
            if not gens:
                problems.append("%s: nothing in the trash for this key" % key)
                continue
            t.generations = gens
        targets.append(t)

    if problems:
        raise TargetError(problems)
    return targets


def plan_create_all(raw_root: Path, archive_root: Path) -> List[Target]:
    """Every raw folder without any archive, partial-free and journal-free."""
    arcs = layout.archive_candidates(archive_root)
    journals = layout.list_journals(archive_root)
    out: List[Target] = []
    for key, raw in layout.raw_candidates(raw_root).items():
        if key in arcs:
            continue
        t = Target(key=key, raw_path=raw)
        if key in journals:
            t.skip = "unfinished %s" % journals[key].get("op")
        out.append(t)
    return out


# --- writing a zip -----------------------------------------------------------

def _walk_files(root: Path) -> List[tuple]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            files.append((full, full.relative_to(root).as_posix()))
    return files


def write_zip(raw_path: Path, dest: Path, *, root_name: str, reporter: Optional[ProgressReporter] = None) -> int:
    """Write a new zip of raw_path at dest (truncating dest), members under root_name/."""
    files = _walk_files(raw_path)
    total = len(files)
    with layout.create_new(dest, "wb") as fh:
        with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for idx, (full, rel) in enumerate(files, start=1):
                if reporter:
                    reporter(idx, total, "zip:write")
                zf.write(full, "%s/%s" % (root_name, rel))
        fh.flush()
        os.fsync(fh.fileno())
    return total


def _place_new(partial: Path, dest: Path) -> None:
    """Move partial to dest without ever overwriting an existing dest.

    Preferred: hard-link partial as dest (fails if dest exists), then drop the
    partial name. Where hard links are not supported, first claim dest with an
    exclusive create (fails if any entry, even a dangling link, exists), then
    replace that empty placeholder with the partial.
    """
    appeared = OperationError("%s appeared while writing; left %s in place" % (dest.name, partial.name))
    try:
        os.link(partial, dest)
    except FileExistsError:
        raise appeared
    except OSError:
        try:
            fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise appeared
        os.close(fd)
        os.replace(partial, dest)
        layout.fsync_dir(dest.parent)
        return
    partial.unlink()
    layout.fsync_dir(dest.parent)


# --- create ------------------------------------------------------------------

def create_one(
    t: Target,
    archive_root: Path,
    record: Recorder,
    *,
    dry_run: bool,
    reporter: Optional[ProgressReporter] = None,
) -> OpResult:
    assert t.raw_path is not None
    dest = archive_root / ("%s.zip" % t.key)
    partial = layout.partial_path(archive_root, t.key)
    if t.skip:
        return OpResult(t.key, True, "skipped: %s" % t.skip)
    if dry_run:
        return OpResult(t.key, True, "would create %s (then verify content)" % dest.name, archive_path=dest)
    archive_root.mkdir(parents=True, exist_ok=True)
    write_zip(t.raw_path, partial, root_name=t.key, reporter=reporter)
    res = verify_archive(partial, t.raw_path, level="content")
    if not res.ok:
        partial.unlink()
        return OpResult(t.key, False, "new archive failed content verify: %s" % res.reason, verify=res)
    _place_new(partial, dest)
    record(t.key, {"last_backup": layout.utcnow(), "last_backup_archive_path": str(dest), "verify": res.as_record(dest)})
    return OpResult(t.key, True, "created %s (content verified)" % dest.name, verify=res, archive_path=dest)


# --- repair ------------------------------------------------------------------

def _step(archive_root: Path, journal: Dict[str, Any], step: str) -> None:
    journal["step"] = step
    layout.write_journal(archive_root, journal)
    if _step_hook is not None:
        _step_hook(str(journal["op"]), str(journal["key"]), step)


def _reached(journal: Dict[str, Any], steps: Sequence[str], step: str) -> bool:
    cur = journal.get("step")
    if cur not in steps:
        return False
    return steps.index(cur) >= steps.index(step)


def _trash_copy(archive: Path, archive_root: Path, key: str) -> Path:
    gen = layout.new_generation(archive_root, key)
    dest = gen / archive.name
    try:
        os.link(archive, dest)
    except OSError:
        shutil.copy2(archive, dest)
        if not filecmp.cmp(str(archive), str(dest), shallow=False):
            raise OperationError("trash copy of %s differs from the archive" % archive.name)
        with open(dest, "rb") as fh:
            os.fsync(fh.fileno())
    layout.fsync_dir(gen)
    return dest


def repair_one(
    t: Target,
    archive_root: Path,
    record: Recorder,
    *,
    dry_run: bool,
    reporter: Optional[ProgressReporter] = None,
) -> OpResult:
    assert t.raw_path is not None and t.archive_path is not None
    partial = layout.partial_path(archive_root, t.key)
    if dry_run:
        return OpResult(
            t.key, True,
            "would rebuild %s from raw into %s, verify content, keep the old archive in %s/%s/<UTC>/, then replace"
            % (t.archive_path.name, partial.name, layout.TRASH_DIR, t.key),
            archive_path=t.archive_path,
        )
    journal = t.journal
    if journal is None:
        journal = {
            "version": 1, "op": "repair", "key": t.key, "step": None,
            "archive_path": str(t.archive_path), "partial_path": str(partial),
            "trash_path": None, "started_at": layout.utcnow(),
        }
        _step(archive_root, journal, "started")
    archive = Path(str(journal["archive_path"]))
    partial = Path(str(journal["partial_path"]))
    res: Optional[VerifyResult] = None
    # Decide from the journal as it was read, before any step below rewrites it.
    old_trash = journal.get("trash_path")
    have_trash = bool(
        _reached(journal, REPAIR_STEPS, "trash_copied") and old_trash and Path(str(old_trash)).is_file()
    )

    if not _reached(journal, REPAIR_STEPS, "replaced"):
        write_zip(t.raw_path, partial, root_name=t.key, reporter=reporter)
        _step(archive_root, journal, "partial_written")
        res = verify_archive(partial, t.raw_path, level="content")
        if not res.ok:
            partial.unlink()
            layout.drop_journal(archive_root, t.key)
            return OpResult(t.key, False, "rebuilt archive failed content verify (%s); old archive unchanged" % res.reason, verify=res)
        _step(archive_root, journal, "partial_verified")
        if not have_trash:
            if not archive.is_file():
                raise OperationError("%s: archive missing before the trash copy; journal kept" % t.key)
            journal["trash_path"] = str(_trash_copy(archive, archive_root, t.key))
        _step(archive_root, journal, "trash_copied")
        os.replace(partial, archive)
        layout.fsync_dir(archive.parent)
        _step(archive_root, journal, "replaced")

    note = ""
    if res is None:
        # Resumed after the swap: the new archive was content-verified before it was
        # put in place. Raw may have changed since, so finish on a whole-archive (crc)
        # check instead of comparing with raw again, and say so.
        res = verify_archive(archive, t.raw_path, level="content")
        if not res.ok:
            crc = verify_archive(archive, level="crc")
            if not crc.ok:
                raise OperationError("%s: archive after replace failed crc verify (%s); journal kept" % (t.key, crc.reason))
            note = "; raw changed since the archive was rebuilt (%s): run repair again to include it" % res.reason
            res = crc
    if not _reached(journal, REPAIR_STEPS, "registry_updated"):
        record(t.key, {"last_repair": layout.utcnow(), "last_repair_trash_path": journal.get("trash_path"),
                       "verify": res.as_record(archive)})
        _step(archive_root, journal, "registry_updated")
    layout.drop_journal(archive_root, t.key)
    trash = journal.get("trash_path")
    return OpResult(t.key, True, "repaired %s (%s verified; old copy in trash)%s" % (archive.name, res.level, note),
                    verify=res, archive_path=archive, trash_path=Path(trash) if trash else None)


# --- remove ------------------------------------------------------------------

def remove_one(t: Target, archive_root: Path, record: Recorder, *, dry_run: bool) -> OpResult:
    assert t.archive_path is not None
    if dry_run:
        return OpResult(t.key, True, "would move %s into %s/%s/<UTC>/" % (t.archive_path.name, layout.TRASH_DIR, t.key),
                        archive_path=t.archive_path)
    journal = t.journal
    if journal is None:
        journal = {
            "version": 1, "op": "remove", "key": t.key, "step": None,
            "archive_path": str(t.archive_path), "trash_path": None, "started_at": layout.utcnow(),
        }
        gen = layout.new_generation(archive_root, t.key)
        journal["trash_path"] = str(gen / t.archive_path.name)
        _step(archive_root, journal, "started")
    archive = Path(str(journal["archive_path"]))
    trash = Path(str(journal["trash_path"]))

    if not _reached(journal, REMOVE_STEPS, "moved"):
        if archive.exists() and not trash.exists():
            trash.parent.mkdir(parents=True, exist_ok=True)
            os.replace(archive, trash)
            layout.fsync_dir(archive.parent)
            layout.fsync_dir(trash.parent)
        elif archive.exists() and trash.exists():
            raise OperationError("%s: both the archive and its trash copy exist; journal kept" % t.key)
        elif not trash.exists():
            raise OperationError("%s: neither the archive nor its trash copy exists; journal kept" % t.key)
        _step(archive_root, journal, "moved")
    if not _reached(journal, REMOVE_STEPS, "registry_updated"):
        record(t.key, {"removed_at": layout.utcnow(), "removed_trash_path": str(trash)})
        _step(archive_root, journal, "registry_updated")
    layout.drop_journal(archive_root, t.key)
    return OpResult(t.key, True, "moved %s to the trash" % archive.name, archive_path=archive, trash_path=trash)


# --- purge -------------------------------------------------------------------

def tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _d, filenames in os.walk(path):
        for n in filenames:
            try:
                total += (Path(dirpath) / n).lstat().st_size
            except OSError:
                pass
    return total


def purge_one(t: Target, archive_root: Path, record: Recorder, *, dry_run: bool) -> OpResult:
    kdir = layout.trash_key_dir(archive_root, t.key)
    if dry_run:
        return OpResult(t.key, True, "would delete %d trash generation(s)" % len(t.generations))
    deleted: List[Path] = []
    for gen in t.generations:
        if gen.is_symlink() or not gen.is_dir() or gen.parent != kdir:
            return OpResult(t.key, False, "refused: %s is not a plain trash folder" % gen, deleted=deleted)
        try:
            shutil.rmtree(gen)
        except OSError as exc:
            partly = " (partly deleted: some files inside it may be gone)" if gen.exists() else ""
            return OpResult(t.key, False, "stopped: could not delete %s%s (%s)" % (gen, partly, exc), deleted=deleted)
        deleted.append(gen)
    try:
        kdir.rmdir()
    except OSError:
        pass
    try:
        record(t.key, {"purged_at": layout.utcnow()})
    except Exception as exc:  # the deletion is done; only the registry note is missing
        return OpResult(t.key, False, "deleted %d trash generation(s), but the registry was not updated (%s)"
                        % (len(deleted), exc), deleted=deleted)
    return OpResult(t.key, True, "deleted %d trash generation(s)" % len(deleted), deleted=deleted)
