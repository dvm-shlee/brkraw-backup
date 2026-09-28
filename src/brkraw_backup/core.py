"""Status scan, JSON registry, status table and legacy-cache migration.

Nothing in this module writes to a raw folder or to an archive. The commands
that change files are in actions.py; verification is in verify.py.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import datetime as _dt
import json
import logging
import os
from pathlib import Path
import pickle
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Callable

from brkraw.core.formatter import format_data
from brkraw.dataclasses.study import Study

from . import layout

logger = logging.getLogger("brkraw")


DEFAULT_REGISTRY_NAME = ".brkraw-backup-registry.json"
ProgressReporter = Callable[[int, int, str], None]


@dataclass(frozen=True)
class DatasetSnapshot:
    key: str
    raw_path: Optional[str]
    archive_path: Optional[str]
    raw_present: bool
    archive_present: bool
    raw_valid: bool
    archive_valid: bool
    raw_scan_count: Optional[int]
    archive_scan_count: Optional[int]
    raw_sw_version: Optional[str]
    archive_sw_version: Optional[str]
    raw_bytes: Optional[int]
    archive_bytes: Optional[int]
    issues: Tuple[str, ...]
    status: str


def _utcnow() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _zip_size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_loader(path: Path) -> Tuple[bool, Optional[int], Optional[str]]:
    try:
        study = Study.from_path(path)
    except Exception:
        return False, None, None
    try:
        scan_count = len(study.avail)
    except Exception:
        scan_count = None
    # Avoid BrukerLoader sw_version parsing here to keep scans lightweight and
    # prevent deep rule selection logs during simple status scans.
    return True, scan_count, None


def scan_datasets(
    raw_root: Path,
    archive_root: Path,
    *,
    reporter: Optional[ProgressReporter] = None,
) -> List[DatasetSnapshot]:
    """Look at the raw and archive folders (names, sizes, a quick brkraw load).

    This is not a verification: use verify.verify_archive for crc/content checks.
    """
    logger.info("Scan start: raw_root=%s archive_root=%s", raw_root, archive_root)
    raw_datasets = layout.raw_candidates(raw_root)
    archive_all = layout.archive_candidates(archive_root)
    # A key with two archives (e.g. <key>.zip and <key>.PvDatasets) is shown as
    # duplicate_archive; the first one (sorted) is used only for the display columns.
    archive_datasets = {k: v[0] for k, v in archive_all.items()}
    journals = layout.list_journals(archive_root)
    partials = layout.partial_files(archive_root)
    logger.info("Discovered candidates: raw=%d archive=%d", len(raw_datasets), len(archive_datasets))

    keys = sorted(set(raw_datasets) | set(archive_datasets) | set(journals))
    snapshots: List[DatasetSnapshot] = []

    total = len(keys)
    for idx, key in enumerate(keys, start=1):
        if reporter:
            reporter(idx, total, "scan:datasets")
        raw_path = raw_datasets.get(key)
        arc_path = archive_datasets.get(key)

        raw_present = raw_path is not None and raw_path.exists()
        arc_present = arc_path is not None and arc_path.exists()

        raw_valid, raw_scans, raw_ver = (False, None, None)
        if raw_present and raw_path is not None:
            raw_valid, raw_scans, raw_ver = _load_loader(raw_path)

        arc_valid, arc_scans, arc_ver = (False, None, None)
        if arc_present and arc_path is not None:
            arc_valid, arc_scans, arc_ver = _load_loader(arc_path)

        issues: List[str] = []
        if raw_present and not raw_valid:
            issues.append("raw_invalid")
        if arc_present and not arc_valid:
            issues.append("archive_corrupt")
        if raw_present and not arc_present:
            issues.append("archive_missing")
        # raw_missing is expected once data is archived; treat it as an issue
        # only when the archive is not readable/valid.
        if not raw_present and arc_present and not arc_valid:
            issues.append("raw_missing")
        if not raw_present and not arc_present and key not in journals:
            issues.append("both_missing")
        if len(archive_all.get(key, [])) > 1:
            issues.append("duplicate_archive")
        if key in journals:
            j = journals[key]
            issues.append("unfinished:%s:%s" % (j.get("op"), j.get("step")))
        if key in partials:
            issues.append("partial_file")
        if raw_present and arc_present and raw_valid and arc_valid:
            if raw_scans is not None and arc_scans is not None and raw_scans != arc_scans:
                issues.append("scan_count_mismatch")
            if raw_ver and arc_ver and raw_ver != arc_ver:
                issues.append("paravision_version_mismatch")

        status = _derive_status(raw_present, arc_present, raw_valid, arc_valid, issues)
        if issues:
            logger.debug("Dataset issues: %s -> %s", key, ",".join(issues))
        snapshots.append(
            DatasetSnapshot(
                key=key,
                raw_path=str(raw_path.resolve(strict=False)) if raw_path else None,
                archive_path=str(arc_path.resolve(strict=False)) if arc_path else None,
                raw_present=raw_present,
                archive_present=arc_present,
                raw_valid=raw_valid,
                archive_valid=arc_valid,
                raw_scan_count=_safe_int(raw_scans),
                archive_scan_count=_safe_int(arc_scans),
                raw_sw_version=raw_ver,
                archive_sw_version=arc_ver,
                raw_bytes=_dir_size_bytes(raw_path) if raw_present and raw_path else None,
                archive_bytes=_zip_size_bytes(arc_path) if arc_present and arc_path else None,
                issues=tuple(issues),
                status=status,
            )
        )

    if reporter:
        reporter(total, total, "scan:done")
    logger.info("Scan done: datasets=%d", len(snapshots))
    return snapshots


def _derive_status(
    raw_present: bool,
    archive_present: bool,
    raw_valid: bool,
    archive_valid: bool,
    issues: Sequence[str],
) -> str:
    if any(i.startswith("unfinished:") for i in issues):
        return "UNFINISHED"
    if "duplicate_archive" in issues:
        return "DUPLICATE"
    if archive_present and not archive_valid:
        return "CORRUPT"
    if raw_present and not raw_valid:
        return "INVALID"
    if raw_present and not archive_present:
        return "MISSING"
    if not raw_present and archive_present and archive_valid:
        return "ARCHIVED"
    if not raw_present and archive_present:
        return "RAW_REMOVED"
    if any(i in {"scan_count_mismatch", "paravision_version_mismatch"} for i in issues):
        return "MISMATCH"
    if raw_present and archive_present and raw_valid and archive_valid:
        return "OK"
    return "UNKNOWN"


def load_registry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "created_at": _utcnow(), "updated_at": _utcnow(), "datasets": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("registry must be a JSON object")
        data.setdefault("datasets", {})
        return data
    except Exception as exc:
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            with path.open("rb") as src, layout.create_new(backup, "wb") as dst:
                shutil.copyfileobj(src, dst)
            logger.warning("Registry unreadable (%s); backed up to %s", path, backup)
        except Exception:
            logger.warning("Registry unreadable (%s): %s", path, exc)
            pass
        return {"version": 1, "created_at": _utcnow(), "updated_at": _utcnow(), "datasets": {}}


def save_registry(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = dict(data)
    payload["updated_at"] = _utcnow()
    with layout.create_new(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)
    logger.debug("Saved registry: %s", path)


def update_registry(
    registry: Dict[str, Any],
    snapshots: Iterable[DatasetSnapshot],
    *,
    raw_root: Path,
    archive_root: Path,
) -> Dict[str, Any]:
    data = dict(registry)
    data.setdefault("version", 1)
    data.setdefault("created_at", _utcnow())
    data.setdefault("datasets", {})
    data["raw_root"] = str(raw_root.resolve(strict=False))
    data["archive_root"] = str(archive_root.resolve(strict=False))
    datasets = dict(data["datasets"])
    now = _utcnow()
    for snap in snapshots:
        entry = datasets.get(snap.key, {})
        if not isinstance(entry, dict):
            entry = {}
        entry.update(asdict(snap))
        entry["last_scan"] = now
        datasets[snap.key] = entry
    data["datasets"] = datasets
    logger.debug("Updated registry entries: %d", len(datasets))
    return data


def set_entry_fields(registry: Dict[str, Any], key: str, fields: Mapping[str, Any]) -> None:
    datasets = registry.get("datasets")
    if not isinstance(datasets, dict):
        datasets = {}
        registry["datasets"] = datasets
    entry = datasets.get(key)
    if not isinstance(entry, dict):
        entry = {"key": key}
    entry.update(dict(fields))
    datasets[key] = entry


def _status_cell(status: str) -> Mapping[str, Any]:
    label_map = {
        "MISSING": "TODO",
        # Raw missing but archive exists (validation may be unknown/failed).
        # Keep JSON status stable, but show friendlier label.
        "RAW_REMOVED": "ARCHIVED",
    }
    label = label_map.get(status, status)
    if status == "OK":
        return {"value": label, "color": "green", "bold": True}
    if status == "ARCHIVED":
        return {"value": label, "color": "green", "bold": True}
    if status == "MISSING":
        return {"value": label, "color": "blue", "bold": True}
    if status in {"CORRUPT", "INVALID", "DUPLICATE", "UNFINISHED"}:
        return {"value": label, "color": "red", "bold": True}
    if status == "MISMATCH":
        return {"value": label, "color": "yellow", "bold": True}
    if status == "RAW_REMOVED":
        return {"value": label, "color": "cyan"}
    return {"value": label, "color": "gray"}


def _format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "-"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if units[idx] in {"B", "KB"}:
        return f"{int(size)}{units[idx]}"
    return f"{size:.1f}{units[idx]}"


def _format_backup_time(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return str(value)
    # Render in local time for operator friendliness.
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d")


def verify_label(entry: Any) -> str:
    """'crc:OK', 'content:FAIL', or '-' from a registry entry's last verify record."""
    if not isinstance(entry, Mapping):
        return "-"
    v = entry.get("verify")
    if not isinstance(v, Mapping) or not v.get("level"):
        return "-"
    return "%s:%s" % (v.get("level"), "OK" if v.get("ok") else "FAIL")


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    # Use ASCII dots to keep monospace width predictable across terminals.
    return text[: max_len - 3] + "..."


def render_scan_table(
    snapshots: Sequence[DatasetSnapshot],
    *,
    max_width: Optional[int] = None,
    registry: Optional[Mapping[str, Any]] = None,
    show_issue_details: bool = False,
) -> str:
    reg_datasets: Mapping[str, Any] = {}
    if registry and isinstance(registry.get("datasets"), Mapping):
        reg_datasets = registry.get("datasets", {})  # type: ignore[assignment]

    rows: List[Dict[str, Any]] = []
    max_key_len = max((len(s.key) for s in snapshots), default=len("DATASET"))
    # Hard cap to keep very long dataset names from blowing up table alignment.
    # (Python format alignment does not truncate, so we must truncate ourselves.)
    KEY_CAP = 45
    key_w = min(max(max_key_len, len("DATASET")), KEY_CAP)

    def _entry(key: str) -> Mapping[str, Any]:
        entry = reg_datasets.get(key)
        return entry if isinstance(entry, Mapping) else {}

    def _verify_at(key: str) -> str:
        v = _entry(key).get("verify")
        if isinstance(v, Mapping):
            return _format_backup_time(v.get("checked_at"))  # type: ignore[arg-type]
        return "-"

    for snap in snapshots:
        raw_scans = "-" if not snap.raw_present else (snap.raw_scan_count if snap.raw_scan_count is not None else "?")
        arc_scans = "-" if not snap.archive_present else (
            snap.archive_scan_count if snap.archive_scan_count is not None else "?"
        )
        issues = ",".join(snap.issues) if snap.issues else ""
        rows.append(
            {
                "key": snap.key,
                "rawn": raw_scans,
                "arcn": arc_scans,
                "rawsz": _format_bytes(snap.raw_bytes) if snap.raw_present else "-",
                "arcsz": _format_bytes(snap.archive_bytes) if snap.archive_present else "-",
                "bkp": _format_backup_time(_entry(snap.key).get("last_backup")),  # type: ignore[arg-type]
                "status": snap.status,
                "verify": verify_label(_entry(snap.key)),
                "vat": _verify_at(snap.key),
                "issues": issues,
            }
        )

    def _cell_text(value: Any) -> str:
        if isinstance(value, Mapping) and "value" in value:
            return str(value.get("value", ""))
        return str(value)

    def _w(title: str, col: str) -> int:
        return max(len(title), max((len(_cell_text(r[col])) for r in rows), default=1))

    gap = "  "
    raw_w = _w("RAW", "rawn")
    arc_w = _w("ARC", "arcn")
    rawsz_w = _w("RAW_SZ", "rawsz")
    arcz_w = _w("ARC_SZ", "arcsz")
    bkp_w = _w("BACKUP_AT", "bkp")
    status_w = _w("STATUS", "status")
    ver_w = _w("VERIFY", "verify")
    vat_w = _w("VERIFY_AT", "vat")

    fixed = len(gap) * 8 + raw_w + arc_w + rawsz_w + arcz_w + bkp_w + status_w + ver_w + vat_w

    if max_width is not None:
        min_key = 20
        max_key_allowed = max(min_key, max_width - fixed)
        key_w = min(key_w, max_key_allowed)

    # Always truncate/pad keys and status using formatter-aware padding.
    # (ANSI styling breaks Python's built-in width calculations.)
    # Issues are not a column; they go on their own line below the row.
    for row in rows:
        key_text = _truncate(str(row.get("key", "")), key_w)
        row["key"] = {"value": key_text, "bold": True, "size": key_w, "align": "left"}
        status_cell = dict(_status_cell(str(row.get("status", "UNKNOWN"))))
        status_cell.update({"size": status_w, "align": "left"})
        row["status"] = status_cell

    template = (
        f"{{key}}{gap}"
        f"{{rawn: >{raw_w}}}{gap}"
        f"{{arcn: >{arc_w}}}{gap}"
        f"{{rawsz: >{rawsz_w}}}{gap}"
        f"{{arcsz: >{arcz_w}}}{gap}"
        f"{{bkp: <{bkp_w}}}{gap}"
        f"{{status}}{gap}"
        f"{{verify: <{ver_w}}}{gap}"
        f"{{vat: <{vat_w}}}"
    )
    header = (
        f"{'DATASET': <{key_w}}{gap}"
        f"{'RAW': >{raw_w}}{gap}"
        f"{'ARC': >{arc_w}}{gap}"
        f"{'RAW_SZ': >{rawsz_w}}{gap}"
        f"{'ARC_SZ': >{arcz_w}}{gap}"
        f"{'BACKUP_AT': <{bkp_w}}{gap}"
        f"{'STATUS': <{status_w}}{gap}"
        f"{'VERIFY': <{ver_w}}{gap}"
        f"{'VERIFY_AT': <{vat_w}}"
    )
    sep = "-" * len(header)
    body_lines: List[str] = []
    for row in rows:
        rendered = format_data(row, template, width=None, on_missing="placeholder")
        if rendered:
            body_lines.append(rendered)
        if not show_issue_details:
            continue
        issues = str(row.get("issues") or "").strip()
        if issues:
            if max_width is not None:
                issues = _truncate(issues, max(0, max_width - 4))
            issues_line = format_data(
                {
                    "label": {"value": "ISSUES", "color": "red", "bold": True},
                    "text": issues,
                },
                "{label}: {text}",
                indent=2,
                width=None,
                on_missing="placeholder",
            )
            if issues_line:
                body_lines.append(issues_line)

    body = "\n".join(body_lines)
    return "\n".join([header, sep, body]) if body else "\n".join([header, sep])


def snapshots_from_registry(registry: Mapping[str, Any]) -> List[DatasetSnapshot]:
    datasets = registry.get("datasets", {})
    if not isinstance(datasets, Mapping):
        return []
    snapshots: List[DatasetSnapshot] = []
    for key in sorted(datasets.keys()):
        entry = datasets.get(key)
        if not isinstance(entry, Mapping):
            continue

        issues_value = entry.get("issues", ())
        if isinstance(issues_value, (list, tuple)):
            issues = tuple(str(x) for x in issues_value)
        elif issues_value is None:
            issues = ()
        else:
            issues = (str(issues_value),)

        def _abs_path(value: object) -> Optional[str]:
            if not isinstance(value, str) or not value.strip():
                return None
            try:
                return str(Path(value).expanduser().resolve(strict=False))
            except Exception:
                return value

        snapshots.append(
            DatasetSnapshot(
                key=str(entry.get("key", key)),
                raw_path=_abs_path(entry.get("raw_path")),
                archive_path=_abs_path(entry.get("archive_path")),
                raw_present=bool(entry.get("raw_present", False)),
                archive_present=bool(entry.get("archive_present", False)),
                raw_valid=bool(entry.get("raw_valid", False)),
                archive_valid=bool(entry.get("archive_valid", False)),
                raw_scan_count=_safe_int(entry.get("raw_scan_count")),
                archive_scan_count=_safe_int(entry.get("archive_scan_count")),
                raw_sw_version=entry.get("raw_sw_version"),
                archive_sw_version=entry.get("archive_sw_version"),
                raw_bytes=_safe_int(entry.get("raw_bytes")),
                archive_bytes=_safe_int(entry.get("archive_bytes")),
                issues=issues,
                status=str(entry.get("status", "UNKNOWN")),
            )
        )
    return snapshots


class _LegacyPlaceholder:
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)


class _LegacyUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):  # noqa: D401
        # The legacy cache pickles objects from brkraw 0.3.x (e.g. brkraw.lib.backup.NamedTuple).
        # We don't import those modules; instead, hydrate them into a generic placeholder.
        return type(f"Legacy_{module.replace('.', '_')}_{name}", (_LegacyPlaceholder,), {})


def load_legacy_cache(path: Path) -> Optional[object]:
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            return _LegacyUnpickler(f).load()
    except Exception:
        return None


def migrate_legacy_cache_to_registry(
    legacy_cache: object,
    registry: Dict[str, Any],
    *,
    archive_root: Path,
    source_path: Path,
    overwrite: bool = False,
    keep_logs: int = 50,
    reporter: Optional[ProgressReporter] = None,
) -> Tuple[Dict[str, Any], int]:
    try:
        raw_n = len(getattr(legacy_cache, "raw_data", []) or [])
        arc_n = len(getattr(legacy_cache, "arc_data", []) or [])
    except Exception:
        raw_n, arc_n = -1, -1
    logger.info("Legacy cache summary: raw_entries=%s archive_entries=%s", raw_n, arc_n)
    datasets = registry.setdefault("datasets", {})
    if not isinstance(datasets, dict):
        datasets = {}
        registry["datasets"] = datasets

    raw_data = getattr(legacy_cache, "raw_data", []) or []
    arc_data = getattr(legacy_cache, "arc_data", []) or []
    log_data = getattr(legacy_cache, "log_data", []) or []

    by_pid: Dict[int, Dict[str, Any]] = {}
    for raw in raw_data:
        try:
            pid = int(getattr(raw, "data_pid", -1))
        except Exception:
            continue
        by_pid.setdefault(pid, {})
        by_pid[pid]["raw"] = {
            "path": getattr(raw, "path", None),
            "garbage": getattr(raw, "garbage", None),
            "removed": getattr(raw, "removed", None),
            "backup": getattr(raw, "backup", None),
        }

    for arc in arc_data:
        try:
            pid = int(getattr(arc, "data_pid", -1))
        except Exception:
            continue
        by_pid.setdefault(pid, {})
        arcs = by_pid[pid].setdefault("archives", [])
        arc_fname = getattr(arc, "path", None)
        arcs.append(
            {
                "filename": arc_fname,
                "path": str((archive_root / arc_fname)) if arc_fname else None,
                "garbage": getattr(arc, "garbage", None),
                "crashed": getattr(arc, "crashed", None),
                "issued": getattr(arc, "issued", None),
            }
        )

    migrated = 0
    items = [item for _, item in sorted(by_pid.items(), key=lambda kv: kv[0])]
    total = len(items)
    if reporter and total:
        reporter(0, total, "migrate:datasets")
    for idx, item in enumerate(items, start=1):
        if reporter:
            reporter(idx, total, "migrate:datasets")
        raw = item.get("raw") or {}
        key = raw.get("path")
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()

        entry = datasets.get(key, {})
        if not isinstance(entry, dict):
            entry = {}

        if "legacy_cache" in entry and not overwrite:
            continue

        legacy_payload: Dict[str, Any] = {
            "source": str(source_path),
            "migrated_at": _utcnow(),
            "raw": raw,
            "archives": item.get("archives", []),
        }
        if keep_logs:
            tail = log_data[-keep_logs:] if isinstance(log_data, list) else []
            legacy_payload["logs_tail"] = [
                {
                    "datetime": getattr(rec, "datetime", None),
                    "method": getattr(rec, "method", None),
                    "message": getattr(rec, "message", None),
                }
                for rec in tail
            ]

        entry["legacy_cache"] = legacy_payload
        entry.setdefault("key", key)
        datasets[key] = entry
        migrated += 1

    registry["datasets"] = datasets
    if reporter and total:
        reporter(total, total, "migrate:done")
    registry.setdefault("migrations", [])
    migrations = registry["migrations"]
    if isinstance(migrations, list):
        migrations.append(
            {
                "type": "legacy_pickle_cache",
                "source": str(source_path),
                "migrated_at": _utcnow(),
                "datasets_migrated": migrated,
            }
        )
    return registry, migrated
