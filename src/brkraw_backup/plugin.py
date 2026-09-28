from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO, Any, Tuple, cast

from brkraw.core import config as config_core

from . import __version__, actions, layout
from .core import (
    DEFAULT_REGISTRY_NAME,
    load_registry,
    load_legacy_cache,
    migrate_legacy_cache_to_registry,
    render_scan_table,
    save_registry,
    scan_datasets,
    set_entry_fields,
    snapshots_from_registry,
    update_registry,
    verify_label,
)
from .verify import LEVELS, verify_archive

logger = logging.getLogger("brkraw")

_BANNER_PRINTED = False
_STDOUT: TextIO = cast(TextIO, sys.__stdout__)
_STDERR: TextIO = cast(TextIO, sys.__stderr__)


def _banner() -> None:
    global _BANNER_PRINTED
    if _BANNER_PRINTED:
        return
    _BANNER_PRINTED = True
    logger.info("brkraw-backup v%s", __version__)


def _make_progress(args: argparse.Namespace):
    def _pick_stream() -> TextIO:
        # Render progress to the same stream as the root logging handler when possible,
        # and clear the line before printing log output to avoid overwriting headers.
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler):
                stream = getattr(handler, "stream", None)
                if stream is sys.stdout or stream is sys.__stdout__:
                    return _STDOUT
                if stream is sys.stderr or stream is sys.__stderr__:
                    return _STDERR
        return _STDERR

    stream = _pick_stream()
    enabled = (
        not bool(getattr(args, "no_progress", False))
        and stream.isatty()
        and logger.isEnabledFor(logging.INFO)
    )
    last_emit = 0.0
    last_line_len = 0
    start = time.time()

    def _label(step: str) -> str:
        return step.split(":", 1)[0] if ":" in step else step

    def reporter(current: int, total: int, step: str) -> None:
        nonlocal last_emit, last_line_len
        if total <= 0:
            return
        now = time.time()
        if now - last_emit < 0.1 and current < total:
            return
        last_emit = now

        label = _label(step)
        width = 24
        frac = min(1.0, max(0.0, current / total))
        filled = int(width * frac)
        bar = "#" * filled + "-" * (width - filled)
        elapsed = max(0.001, time.time() - start)
        rate = current / elapsed if current > 0 else 0.0
        remaining = max(0, total - current)
        eta = int(remaining / rate) if rate > 0 else -1
        eta_txt = f"{eta}s" if eta >= 0 else "?"
        line = f"{label} [{bar}] {current}/{total} ETA {eta_txt}"
        pad = " " * max(0, last_line_len - len(line))
        last_line_len = len(line)
        stream.write("\r" + line + pad)
        stream.flush()

    def done() -> None:
        if not enabled:
            return
        # Clear progress line to avoid leaving partial characters on screen.
        stream.write("\r" + (" " * last_line_len) + "\r\n")
        stream.flush()

    if not enabled:
        def reporter_noop(current: int, total: int, step: str) -> None:
            return

        def done_noop() -> None:
            return

        return reporter_noop, done_noop
    return reporter, done


def _get_backup_paths_from_config(*, root: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    cfg = config_core.resolve_config(root=root)
    backup_cfg = cfg.get("backup", {})
    if not isinstance(backup_cfg, dict):
        return None, None
    raw = backup_cfg.get("rawdata")
    arc = backup_cfg.get("archive")
    raw = raw.strip() if isinstance(raw, str) else None
    arc = arc.strip() if isinstance(arc, str) else None
    return raw or None, arc or None


def _configured_print_width(*, root: Optional[str]) -> Optional[int]:
    cfg = config_core.load_config(root=root)
    if not isinstance(cfg, dict):
        return None
    logging_cfg = cfg.get("logging", {})
    if not isinstance(logging_cfg, dict):
        return None
    value = logging_cfg.get("print_width")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _effective_print_width(*, root: Optional[str]) -> Optional[int]:
    configured = _configured_print_width(root=root)
    if configured:
        return configured
    if _STDOUT.isatty() or _STDERR.isatty():
        cols: int = shutil.get_terminal_size(fallback=(80, 0)).columns
        return cols if cols > 0 else 80
    # Non-interactive fallback: still cap output to prevent wrapping in logs.
    return 80


def _resolve_paths(args: argparse.Namespace, *, need_raw: bool = True, need_archive: bool = True) -> tuple[Path, Path]:
    raw_cli = getattr(args, "raw_root", None) or getattr(args, "rawdata", None)
    arc_cli = getattr(args, "archive_root", None) or getattr(args, "archive", None)
    raw_cfg, arc_cfg = _get_backup_paths_from_config(root=getattr(args, "root", None))

    raw_value = raw_cli or raw_cfg
    arc_value = arc_cli or arc_cfg

    missing: list[str] = []
    if need_raw and not raw_value:
        missing.append("backup.rawdata (--rawdata)")
    if need_archive and not arc_value:
        missing.append("backup.archive (--archive)")
    if missing:
        hint = (
            "Pass --rawdata/--archive, "
            "or set them via: brkraw backup init <raw_root> <archive_root>."
        )
        raise ValueError(f"Missing required path(s): {', '.join(missing)}. {hint}")

    def _resolve(p: str) -> Path:
        # Use realpath-like resolution so symlinks are handled consistently.
        # strict=False allows resolution even if the directory doesn't exist yet.
        return Path(p).expanduser().resolve(strict=False)

    raw_path = _resolve(raw_value) if raw_value else Path()
    arc_path = _resolve(arc_value) if arc_value else Path()
    return raw_path, arc_path


def _paths(args: argparse.Namespace, *, need_raw: bool) -> Tuple[Optional[Path], Path]:
    """(raw_root or None, archive_root). Raw is resolved when configured even if not needed."""
    raw, arc = _resolve_paths(args, need_raw=need_raw, need_archive=True)
    raw_value = getattr(args, "rawdata", None) or _get_backup_paths_from_config(root=getattr(args, "root", None))[0]
    raw_root = raw if raw_value else None
    _check_write_places(args, raw_root, arc)
    return raw_root, arc


def _check_write_places(args: argparse.Namespace, raw_root: Optional[Path], archive_root: Path) -> None:
    """Everything brkraw-backup writes lives in the archive folder; keep that apart from raw."""
    bad = layout.check_file_name(getattr(args, "registry", DEFAULT_REGISTRY_NAME))
    if bad:
        raise ValueError("--registry %s" % bad)
    bad = layout.roots_problem(raw_root, archive_root)
    if bad:
        raise ValueError(bad)


def _maybe_prompt_save_backup_paths(
    args: argparse.Namespace,
    *,
    raw_root: Path,
    archive_root: Path,
) -> None:
    if getattr(args, "no_config_prompt", False):
        return
    if not sys.stdin.isatty():
        return

    raw_cli = getattr(args, "raw_root", None) or getattr(args, "rawdata", None)
    arc_cli = getattr(args, "archive_root", None) or getattr(args, "archive", None)
    if not raw_cli or not arc_cli:
        return

    config = config_core.load_config(root=getattr(args, "root", None))
    if config is None:
        return

    backup_cfg = config.get("backup")
    if not isinstance(backup_cfg, dict):
        backup_cfg = {}

    missing_keys: list[str] = []
    if not (isinstance(backup_cfg.get("rawdata"), str) and backup_cfg.get("rawdata", "").strip()):
        missing_keys.append("backup.rawdata")
    if not (isinstance(backup_cfg.get("archive"), str) and backup_cfg.get("archive", "").strip()):
        missing_keys.append("backup.archive")
    if not missing_keys:
        return

    raw_root = raw_root.resolve(strict=False)
    archive_root = archive_root.resolve(strict=False)

    prompt = (
        f"Config file exists but {', '.join(missing_keys)} not set.\n"
        f"Save current paths to config.yaml?\n"
        f"  rawdata : {raw_root}\n"
        f"  archive : {archive_root}\n"
        f"[y/N]: "
    )
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer not in {"y", "yes"}:
        return

    backup_cfg.setdefault("rawdata", str(raw_root))
    backup_cfg.setdefault("archive", str(archive_root))
    config["backup"] = backup_cfg
    config_core.write_config(config, root=getattr(args, "root", None))
    logger.info("Saved backup.rawdata/archive to config.yaml.")


# --- argument helpers ----------------------------------------------------------

_ROOT_HELP = "Override brkraw config root directory (default: BRKRAW_CONFIG_HOME or ~/.brkraw)."


def _add_init_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("raw_root", help="Directory containing raw datasets (subdirs).")
    parser.add_argument("archive_root", help="Directory to store dataset zip archives.")
    parser.add_argument("--root", help=_ROOT_HELP)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config keys when present.",
    )


def _add_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rawdata", dest="rawdata", help="Raw folder for this command (default: config backup.rawdata).")
    parser.add_argument("--archive", dest="archive", help="Archive folder for this command (default: config backup.archive).")
    parser.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY_NAME,
        help=f"Registry filename stored under the archive folder (default: {DEFAULT_REGISTRY_NAME}).",
    )
    parser.add_argument("--root", help=_ROOT_HELP)
    parser.add_argument(
        "--no-config-prompt",
        action="store_true",
        help="Disable interactive prompt to save missing backup paths into config.yaml.",
    )
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bar rendering.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing.")


def _add_migrate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("raw_root", nargs="?", help="Directory containing raw datasets (subdirs).")
    parser.add_argument("archive_root", nargs="?", help="Directory to store dataset zip archives.")
    parser.add_argument("--rawdata", dest="rawdata", help="Override config backup.rawdata for this command.")
    parser.add_argument("--archive", dest="archive", help="Override config backup.archive for this command.")
    parser.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY_NAME,
        help=f"Registry filename stored under archive_root (default: {DEFAULT_REGISTRY_NAME}).",
    )
    parser.add_argument("--root", help=_ROOT_HELP)
    parser.add_argument(
        "--no-config-prompt",
        action="store_true",
        help="Disable interactive prompt to save missing backup paths into config.yaml.",
    )
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bar rendering.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated; write nothing.")


def _recorder(registry: dict, registry_path: Path, *, dry_run: bool) -> Callable[[str, Dict[str, Any]], None]:
    def record(key: str, fields: Dict[str, Any]) -> None:
        if dry_run:
            return
        set_entry_fields(registry, key, fields)
        save_registry(registry_path, registry)

    return record


def _report_problems(problems: List[str]) -> int:
    for p in problems:
        logger.error("%s", p)
    logger.error("Nothing was changed.")
    return 2


def _log_verify(key: str, res) -> None:
    if res.ok:
        logger.info("%s: verify %s OK (%s files)", key, res.level, res.details.get("archive_files", "?"))
        return
    logger.error("%s: verify %s FAIL: %s", key, res.level, res.reason)
    for reason in res.reasons:
        names = res.details.get(reason)
        if isinstance(names, list) and names:
            logger.error("  %s: %s", reason, ", ".join(str(n) for n in names))


# --- commands --------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    _banner()
    raw_root = str(Path(args.raw_root).expanduser().resolve(strict=False))
    archive_root = str(Path(args.archive_root).expanduser().resolve(strict=False))

    config_core.ensure_initialized(root=args.root, create_config=True, exist_ok=True)
    config = config_core.load_config(root=args.root) or {}

    backup_cfg = config.get("backup")
    if not isinstance(backup_cfg, dict):
        backup_cfg = {}

    changed = False
    for key, value in (("rawdata", raw_root), ("archive", archive_root)):
        existing = backup_cfg.get(key)
        if isinstance(existing, str) and existing.strip() and not args.force:
            continue
        backup_cfg[key] = value
        changed = True

    config["backup"] = backup_cfg
    if changed:
        config_core.write_config(config, root=args.root)
        logger.info("Saved backup paths to config.yaml (section: backup).")
    else:
        logger.info("Config already has backup paths; skipped (use --force to overwrite).")
    return 0


def _expand_status_tokens(tokens: set) -> set:
    expanded = set()
    for token in tokens:
        if token in {"TODO", "NEED_BACKUP"}:
            expanded.add("MISSING")
        elif token == "ARCHIVED":
            expanded.update({"ARCHIVED", "RAW_REMOVED"})
        else:
            expanded.add(token)
    return expanded


def cmd_status(args: argparse.Namespace) -> int:
    _banner()
    try:
        raw_root, archive_root = _paths(args, need_raw=bool(args.scan))
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    registry_path = archive_root / args.registry
    registry = load_registry(registry_path)

    if args.scan:
        assert raw_root is not None
        _maybe_prompt_save_backup_paths(args, raw_root=raw_root, archive_root=archive_root)
        reporter, done = _make_progress(args)
        snapshots = scan_datasets(raw_root, archive_root, reporter=reporter)
        done()
        if not args.dry_run:
            registry = update_registry(registry, snapshots, raw_root=raw_root, archive_root=archive_root)
            save_registry(registry_path, registry)
    else:
        snapshots = snapshots_from_registry(registry)
        # Unfinished repair/remove work is read live, so it shows even without --scan.
        journals = layout.list_journals(archive_root)
        by_key = {s.key: s for s in snapshots}
        for key, j in journals.items():
            tag = "unfinished:%s:%s" % (j.get("op"), j.get("step"))
            snap = by_key.get(key)
            if snap is None:
                snap = snapshots_from_registry({"datasets": {key: {"key": key}}})[0]
            issues = tuple(i for i in snap.issues if not i.startswith("unfinished:")) + (tag,)
            by_key[key] = dataclasses.replace(snap, issues=issues, status="UNFINISHED")
        snapshots = [by_key[k] for k in sorted(by_key)]
        if not snapshots:
            logger.info("Registry is empty: %s (run `brkraw backup status --scan`).", registry_path)
            return 0

    datasets = registry.get("datasets", {}) if isinstance(registry.get("datasets"), dict) else {}
    if args.keys:
        wanted = set(args.keys)
        unknown = sorted(wanted - {s.key for s in snapshots})
        for k in unknown:
            logger.warning("%s: not found", k)
        snapshots = [s for s in snapshots if s.key in wanted]
    if args.issues:
        snapshots = [
            s for s in snapshots
            if s.status not in {"OK", "ARCHIVED"} or verify_label(datasets.get(s.key)).endswith(":FAIL")
        ]
        if not snapshots:
            logger.info("No issues found.")
            return 0
    if args.status:
        include = _expand_status_tokens({t.strip().upper() for t in args.status.split(",") if t.strip()})
        snapshots = [s for s in snapshots if s.status.upper() in include]

    width = _effective_print_width(root=args.root)
    logger.info(
        "%s",
        render_scan_table(
            snapshots,
            max_width=width,
            registry=registry,
            show_issue_details=bool(args.issues) or logger.isEnabledFor(logging.DEBUG),
        ),
    )
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    _banner()
    try:
        raw_root, archive_root = _paths(args, need_raw=True)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    assert raw_root is not None
    _maybe_prompt_save_backup_paths(args, raw_root=raw_root, archive_root=archive_root)
    if args.keys:
        try:
            targets = actions.resolve_targets("create", args.keys, raw_root=raw_root, archive_root=archive_root)
        except actions.TargetError as exc:
            return _report_problems(exc.problems)
    else:
        targets = actions.plan_create_all(raw_root, archive_root)
    if not targets:
        logger.info("Nothing to create: every raw folder already has an archive.")
        return 0

    registry_path = archive_root / args.registry
    registry = load_registry(registry_path)
    record = _recorder(registry, registry_path, dry_run=args.dry_run)
    failed = 0
    reporter, done = _make_progress(args)
    for t in targets:
        res = actions.create_one(t, archive_root, record, dry_run=args.dry_run, reporter=reporter)
        if res.ok:
            logger.info("%s: %s", t.key, res.message)
        else:
            failed += 1
            logger.error("%s: %s", t.key, res.message)
            if res.verify is not None:
                _log_verify(t.key, res.verify)
    done()
    return 1 if failed else 0


def cmd_verify(args: argparse.Namespace) -> int:
    _banner()
    needs_raw = args.level in ("list", "content")
    try:
        raw_root, archive_root = _paths(args, need_raw=needs_raw)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    if not args.keys and not args.all:
        logger.error("Name at least one KEY, or pass --all.")
        return 2
    if args.keys and args.all:
        logger.error("Pass KEY names or --all, not both.")
        return 2
    if args.path is not None and len(args.keys) != 1:
        logger.error("--path needs exactly one KEY.")
        return 2

    arcs = layout.archive_candidates(archive_root)
    raws = layout.raw_candidates(raw_root) if raw_root is not None else {}
    jobs: List[Tuple[str, Path, Optional[Path]]] = []
    problems: List[str] = []
    skipped: List[str] = []
    if args.all:
        for key, paths in arcs.items():
            raw = raws.get(key)
            if needs_raw and raw is None:
                skipped.append(key)
                continue
            for p in paths:
                jobs.append((key if len(paths) == 1 else "%s (%s)" % (key, p.name), p, raw))
    else:
        for key in dict.fromkeys(args.keys):
            bad = layout.check_key(key)
            if bad:
                problems.append("%s: %s" % (key, bad))
                continue
            paths = list(arcs.get(key, []))
            if args.path is not None:
                chosen = Path(args.path).expanduser()
                if not chosen.is_absolute():
                    chosen = archive_root / chosen
                paths = [p for p in paths if p.resolve(strict=False) == chosen.resolve(strict=False)]
                if not paths:
                    problems.append("%s: --path is not an archive of this key: %s" % (key, args.path))
                    continue
            if not paths:
                problems.append("%s: no archive with this name" % key)
                continue
            if len(paths) > 1:
                problems.append("%s: duplicate_archive (%s); pass --path to choose one" % (key, ", ".join(p.name for p in paths)))
                continue
            raw = raws.get(key)
            if needs_raw and raw is None:
                problems.append("%s: raw folder missing; level %s needs raw (use --level crc)" % (key, args.level))
                continue
            jobs.append((key, paths[0], raw))
    if problems:
        for p in problems:
            logger.error("%s", p)
        return 2
    for key in skipped:
        logger.warning("%s: skipped, raw folder missing (level %s needs raw)", key, args.level)
    if not jobs:
        logger.info("No archives to verify.")
        return 0

    registry_path = archive_root / args.registry
    registry = load_registry(registry_path)
    failed = 0
    for label, path, raw in jobs:
        res = verify_archive(path, raw, level=args.level)
        _log_verify(label, res)
        if not res.ok:
            failed += 1
        if not args.dry_run and label in arcs:
            set_entry_fields(registry, label, {"verify": res.as_record(path)})
    if not args.dry_run and jobs:
        save_registry(registry_path, registry)
    return 1 if failed else 0


def _run_changes(args: argparse.Namespace, op: str) -> int:
    _banner()
    try:
        raw_root, archive_root = _paths(args, need_raw=(op == "repair"))
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    try:
        targets = actions.resolve_targets(
            op, args.keys, raw_root=raw_root, archive_root=archive_root, path=getattr(args, "path", None)
        )
    except actions.TargetError as exc:
        return _report_problems(exc.problems)

    registry_path = archive_root / args.registry
    registry = load_registry(registry_path)
    record = _recorder(registry, registry_path, dry_run=args.dry_run)
    reporter, done = _make_progress(args)
    for t in targets:
        if op == "remove" and t.raw_path is None and not args.dry_run:
            logger.warning("%s: raw folder is missing; the trash copy will be the only copy.", t.key)
        try:
            if op == "repair":
                res = actions.repair_one(t, archive_root, record, dry_run=args.dry_run, reporter=reporter)
            else:
                res = actions.remove_one(t, archive_root, record, dry_run=args.dry_run)
        except Exception as exc:  # the journal stays; the next run finishes the work
            done()
            logger.error("%s: %s stopped: %s", t.key, op, exc)
            logger.error("The journal was kept. Run `brkraw backup %s %s` again to finish.", op, t.key)
            rest = [x.key for x in targets[targets.index(t) + 1:]]
            if rest:
                logger.error("Not started: %s", ", ".join(rest))
            return 1
        if not res.ok:
            done()
            logger.error("%s: %s", t.key, res.message)
            if res.verify is not None:
                _log_verify(t.key, res.verify)
            rest = [x.key for x in targets[targets.index(t) + 1:]]
            if rest:
                logger.error("Not started: %s", ", ".join(rest))
            return 1
        logger.info("%s: %s", t.key, res.message)
    done()
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    return _run_changes(args, "repair")


def cmd_remove(args: argparse.Namespace) -> int:
    return _run_changes(args, "remove")


def cmd_purge(args: argparse.Namespace) -> int:
    _banner()
    try:
        _, archive_root = _paths(args, need_raw=False)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    try:
        targets = actions.resolve_targets("purge", args.keys, raw_root=None, archive_root=archive_root)
    except actions.TargetError as exc:
        return _report_problems(exc.problems)

    total = 0
    for t in targets:
        for gen in t.generations:
            size = actions.tree_bytes(gen)
            total += size
            logger.info("%s: %s (%d bytes)", t.key, gen, size)
    logger.info("Total: %d trash generation(s), %d bytes.", sum(len(t.generations) for t in targets), total)
    if args.dry_run:
        logger.info("Dry run: nothing deleted.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            logger.error("Permanent delete needs confirmation: pass --yes or run in a terminal. Nothing deleted.")
            return 2
        try:
            answer = input("Type 'yes' to delete these permanently: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            logger.info("Cancelled; nothing deleted.")
            return 2

    registry_path = archive_root / args.registry
    registry = load_registry(registry_path)
    record = _recorder(registry, registry_path, dry_run=False)
    for t in targets:
        res = actions.purge_one(t, archive_root, record, dry_run=False)
        for gen in res.deleted:
            logger.info("%s: deleted %s", t.key, gen)
        if not res.ok:
            logger.error("%s: %s", t.key, res.message)
            rest = [x.key for x in targets[targets.index(t) + 1:]]
            if rest:
                logger.error("Not started: %s", ", ".join(rest))
            return 1
        logger.info("%s: %s", t.key, res.message)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    _banner()
    try:
        raw_root, archive_root = _resolve_paths(args, need_raw=not bool(args.no_scan), need_archive=True)
        _check_write_places(args, None if args.no_scan else raw_root, archive_root)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    registry_path = archive_root / args.registry

    if not args.no_scan:
        _maybe_prompt_save_backup_paths(args, raw_root=raw_root, archive_root=archive_root)
    legacy_path = Path(args.old_cache).expanduser()
    if not legacy_path.is_absolute():
        legacy_path = archive_root / legacy_path

    if args.old_cache == ".brk-backup_cache":
        logger.info("Using default legacy cache path (relative to archive_root).")
    logger.info("Archive root: %s", archive_root)
    logger.info("Command: brkraw backup migrate")
    logger.info("Migrating legacy cache -> registry")
    logger.info("Legacy cache: %s", legacy_path)
    logger.info("Registry: %s", registry_path)
    legacy = load_legacy_cache(legacy_path)
    if legacy is None:
        logger.error(
            "Legacy cache not found or unreadable: %s (tip: pass --old-cache /path/to/.brk-backup_cache)",
            legacy_path,
        )
        return 2

    registry = load_registry(registry_path)
    try:
        datasets = registry.get("datasets", {})
        existing = len(datasets) if isinstance(datasets, dict) else 0
    except Exception:
        existing = 0
    logger.info("Registry loaded: datasets=%d", existing)
    reporter, done = _make_progress(args)
    registry, migrated = migrate_legacy_cache_to_registry(
        legacy,
        registry,
        archive_root=archive_root,
        source_path=legacy_path,
        overwrite=bool(args.overwrite),
        keep_logs=int(args.keep_logs),
        reporter=reporter,
    )
    done()

    if not args.no_scan:
        reporter, done = _make_progress(args)
        snapshots = scan_datasets(raw_root, archive_root, reporter=reporter)
        done()
        registry = update_registry(registry, snapshots, raw_root=raw_root, archive_root=archive_root)
        width = _effective_print_width(root=args.root)
        logger.info(
            "%s",
            render_scan_table(
                snapshots,
                max_width=width,
                registry=registry,
                show_issue_details=logger.isEnabledFor(logging.DEBUG),
            ),
        )

    if args.dry_run:
        logger.info("Dry run: registry not written (%d entries would migrate).", migrated)
        return 0
    save_registry(registry_path, registry)
    logger.info("Migrated %d dataset entries from %s", migrated, legacy_path.name)
    return 0


def cmd_about(args: argparse.Namespace) -> int:
    _banner()
    logger.info("python: %s", sys.executable)
    logger.info("brkraw_backup: %s", __file__)
    cfg_root = config_core.resolve_root(getattr(args, "root", None))
    logger.info("config root: %s", cfg_root)
    raw_cfg, arc_cfg = _get_backup_paths_from_config(root=getattr(args, "root", None))
    logger.info("config backup.rawdata: %s", raw_cfg or "-")
    logger.info("config backup.archive: %s", arc_cfg or "-")
    return 0


_BACKUP_DESCRIPTION = """\
Archive raw ParaVision study folders as zip files and keep them verified.

main commands:
  status    show the recorded state (--scan looks again, --issues shows problems only)
  create    make archives for raw folders that have none (never touches an existing archive)
  verify    check archives: --level list | crc (default) | content
  repair    rebuild one archive from raw, keeping the old one in the trash
  remove    move an archive into the trash
  purge     permanently delete trash copies (asks for confirmation)
  init      save the raw and archive folders in brkraw's config

advanced: migrate (import a 0.3.x cache), about (versions and paths)

brkraw-backup never deletes raw folders.
"""


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[name-defined]
    backup_parser = subparsers.add_parser(
        "backup",
        help="Archive raw datasets as zip files and keep them verified.",
        description=_BACKUP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = backup_parser.add_subparsers(dest="backup_command", metavar="command")

    st = sub.add_parser("status", help="Show recorded state; --scan looks again, --issues shows problems only.")
    st.add_argument("keys", nargs="*", metavar="KEY", help="Only these datasets.")
    _add_path_args(st)
    st.add_argument("--scan", action="store_true", help="Look at the raw and archive folders again and update the registry.")
    st.add_argument("--issues", action="store_true", help="Show only datasets with a problem (and the problem).")
    st.add_argument("--status", help="Filter by status (comma-separated), e.g. OK,ARCHIVED,TODO,UNFINISHED.")
    st.set_defaults(func=cmd_status, parser=st)

    cr = sub.add_parser("create", help="Create archives for raw folders without one (never changes an existing archive).")
    cr.add_argument("keys", nargs="*", metavar="KEY", help="Only these raw folders (default: every raw folder without an archive).")
    _add_path_args(cr)
    cr.set_defaults(func=cmd_create, parser=cr)

    ve = sub.add_parser("verify", help="Verify archives (list, crc or content).")
    ve.add_argument("keys", nargs="*", metavar="KEY", help="Archives to verify.")
    ve.add_argument("--all", action="store_true", help="Verify every archive in the archive folder.")
    ve.add_argument("--level", choices=list(LEVELS), default="crc",
                    help="list: names and sizes vs raw; crc: read every member (default, no raw needed); "
                         "content: crc plus raw file CRC-32 comparison.")
    ve.add_argument("--path", help="Which archive to use when a KEY has two (e.g. .zip and .PvDatasets).")
    _add_path_args(ve)
    ve.set_defaults(func=cmd_verify, parser=ve)

    rp = sub.add_parser("repair", help="Rebuild archives from raw; the old archive is kept in the trash.")
    rp.add_argument("keys", nargs="*", metavar="KEY", help="Archives to rebuild (at least one).")
    rp.add_argument("--path", help="Which archive to use when a KEY has two.")
    _add_path_args(rp)
    rp.set_defaults(func=cmd_repair, parser=rp)

    rm = sub.add_parser("remove", help="Move archives into the trash (nothing is deleted).")
    rm.add_argument("keys", nargs="*", metavar="KEY", help="Archives to move (at least one).")
    rm.add_argument("--path", help="Which archive to use when a KEY has two.")
    _add_path_args(rm)
    rm.set_defaults(func=cmd_remove, parser=rm)

    pg = sub.add_parser("purge", help="Permanently delete trash copies of the named keys.")
    pg.add_argument("keys", nargs="*", metavar="KEY", help="Keys whose trash copies to delete (at least one).")
    pg.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
    _add_path_args(pg)
    pg.set_defaults(func=cmd_purge, parser=pg)

    init_p = sub.add_parser("init", help="Register raw/archive paths into brkraw config.yaml.")
    _add_init_args(init_p)
    init_p.set_defaults(func=cmd_init, parser=init_p)

    mig_p = sub.add_parser("migrate", help="(advanced) Migrate a legacy .brk-backup_cache into the JSON registry.")
    _add_migrate_args(mig_p)
    mig_p.add_argument(
        "--old-cache",
        default=".brk-backup_cache",
        help="Legacy pickle cache filename or path (default: .brk-backup_cache under archive_root).",
    )
    mig_p.add_argument("--overwrite", action="store_true", help="Overwrite existing legacy_cache entries.")
    mig_p.add_argument("--keep-logs", type=int, default=50, help="Keep last N legacy log records (default: 50).")
    mig_p.add_argument("--no-scan", action="store_true", help="Skip a post-migration scan/update.")
    mig_p.set_defaults(func=cmd_migrate, parser=mig_p)

    about_p = sub.add_parser("about", help="(advanced) Show plugin version and config paths.")
    about_p.add_argument("--root", help=_ROOT_HELP)
    about_p.set_defaults(func=cmd_about, parser=about_p)

    backup_parser.set_defaults(
        func=lambda args: (args.parser.print_help() or 2),  # type: ignore[attr-defined]
        parser=backup_parser,
    )
