"""brkraw-backup 0.2.0 contract tests (T1-T10 of the WI-0012 design, BRK-0052/BRK-0053).

Synthetic data only (tmp_path). Every failure case checks that the original
archive bytes and the raw folder are unchanged.
"""
import ast
import hashlib
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import damage as D
import fixtures as F
from brkraw.cli.main import main
from brkraw_backup import actions, layout, plugin, verify

TWO_D = "1/pdata/1/2dseq"
SRC = Path(__file__).resolve().parents[1] / "src" / "brkraw_backup"


# --- helpers -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BRKRAW_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(actions, "_step_hook", None)


@pytest.fixture
def env(tmp_path):
    raw = tmp_path / "raw"
    arc = tmp_path / "archive"
    raw.mkdir()
    arc.mkdir()
    return SimpleNamespace(tmp=tmp_path, raw=raw, arc=arc, cfg=tmp_path / "config")


def bk(env, cmd, *args):
    argv = ["backup", cmd, *[str(a) for a in args],
            "--rawdata", str(env.raw), "--archive", str(env.arc), "--root", str(env.cfg),
            "--no-config-prompt", "--no-progress"]
    return main(argv)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def state(root):
    """rel path -> sha256 / 'dir' / 'link:<target>' for everything under root (links not followed)."""
    root = Path(root)
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                out[rel] = "link:" + os.readlink(p)
            elif p.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = sha(p)
    return out


def changed(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def make(env, key="S1", scans=(1,)):
    return F.make_study(env.raw, key, scans=scans)


def created(env, key="S1", scans=(1,)):
    study = make(env, key, scans)
    assert bk(env, "create", key) == 0
    return study, env.arc / ("%s.zip" % key)


def registry(env):
    return json.loads((env.arc / ".brkraw-backup-registry.json").read_text())


def generations(env, key="S1"):
    return layout.trash_generations(env.arc, key)


# --- create / status basics --------------------------------------------------

def test_create_all_missing_verifies_content_and_records_it(env, caplog):
    make(env, "S1")
    make(env, "S2", scans=(1, 2))
    raw_before = state(env.raw)
    with caplog.at_level(logging.INFO):
        assert bk(env, "create") == 0
    assert state(env.raw) == raw_before
    for key in ("S1", "S2"):
        z = env.arc / ("%s.zip" % key)
        assert verify.verify_archive(z, env.raw / key, level="content").ok
        v = registry(env)["datasets"][key]["verify"]
        assert v["level"] == "content" and v["ok"] is True
    assert not list(env.arc.glob("*.partial"))
    assert "content verified" in caplog.text


def test_create_fails_cleanly_when_raw_changes_during_verify(env, monkeypatch):
    study = make(env)
    real = verify.file_crc32
    calls = {"n": 0}
    target = study / TWO_D

    def crc_then_edit(path, *a, **k):
        # hash first, then change that same file with the same size and restore
        # its mtime: only the ctime check can see this (wi-0055-choi-1)
        value = real(path, *a, **k)
        calls["n"] += 1
        if Path(path) == target:
            F.flip_byte_same_size(target, 9)
        return value

    monkeypatch.setattr(verify, "file_crc32", crc_then_edit)
    assert bk(env, "create", "S1") == 1
    assert [p.name for p in env.arc.iterdir()] in ([], [".brkraw-backup-registry.json"])


# --- wi-0055-choi-1 findings -------------------------------------------------------

def test_choi_partial_link_never_writes_through(env):
    study = make(env)
    raw_b = state(env.raw)
    victim = study / "1/acqp"
    (env.arc / "S1.zip.partial").symlink_to(victim)
    assert bk(env, "create", "S1") == 0
    assert state(env.raw) == raw_b
    _, z = study, env.arc / "S1.zip"
    os.link(victim, env.arc / "S1.zip.partial")                # hard link this time
    F.make_study(env.raw, "S1", scans=(2,))
    raw_b = state(env.raw)
    assert bk(env, "repair", "S1") == 0
    assert state(env.raw) == raw_b
    assert verify.verify_archive(z, study, level="content").ok


def test_choi_registry_and_archive_places_are_kept_apart_from_raw(env):
    study = make(env)
    raw_b = state(env.raw)
    for reg in ("../raw/S1/subject", str(study / "subject"), "sub/x.json", ".."):
        assert bk(env, "status", "--scan", "--registry", reg) == 2
        assert bk(env, "create", "--registry", reg) == 2
    inside = SimpleNamespace(tmp=env.tmp, raw=env.raw, arc=study, cfg=env.cfg)
    assert bk(inside, "create") == 2
    assert bk(inside, "status", "--scan") == 2
    same = SimpleNamespace(tmp=env.tmp, raw=env.raw, arc=env.raw, cfg=env.cfg)
    assert bk(same, "create") == 2
    assert state(env.raw) == raw_b


@pytest.mark.parametrize("field,value", [
    ("archive_path", "RAW/S1/1/method"),
    ("partial_path", "RAW/S1/1/acqp"),
    ("trash_path", "RAW/S1/1/reco"),
    ("key", "S2"),
    ("step", "bogus"),
])
def test_choi_forged_journal_is_refused(env, field, value):
    study, z = created(env)
    good = {
        "version": 1, "op": "repair", "key": "S1", "step": "trash_copied",
        "archive_path": str(z), "partial_path": str(layout.partial_path(env.arc, "S1")),
        "trash_path": str(layout.trash_key_dir(env.arc, "S1") / "20260101T000000000000Z" / "S1.zip"),
    }
    good[field] = value.replace("RAW", str(env.raw))

    def put(j):                                               # always as S1's journal file
        layout.journal_dir(env.arc).mkdir(exist_ok=True)
        layout.journal_path(env.arc, "S1").write_text(json.dumps(j))

    put(good)
    before = state(env.tmp)
    assert bk(env, "repair", "S1") == 2
    assert state(env.tmp) == before
    good["op"] = "remove"
    put(good)
    before = state(env.tmp)
    assert bk(env, "remove", "S1") == 2
    assert state(env.tmp) == before


def test_choi_resume_after_replace_with_changed_raw_finishes(env, monkeypatch, caplog):
    study, z = created(env)
    F.make_study(env.raw, "S1", scans=(2,))

    def hook(op, key, step):
        if step == "replaced":
            raise Stop(step)

    monkeypatch.setattr(actions, "_step_hook", hook)
    assert bk(env, "repair", "S1") == 1
    monkeypatch.setattr(actions, "_step_hook", None)
    F.make_study(env.raw, "S1", scans=(3,))                   # raw changes before the rerun
    with caplog.at_level(logging.INFO):
        assert bk(env, "repair", "S1") == 0
    assert "raw changed since" in caplog.text
    assert layout.read_journal(env.arc, "S1") is None
    assert registry(env)["datasets"]["S1"]["verify"]["level"] == "crc"
    assert bk(env, "repair", "S1") == 0                       # and a new repair takes scan 3 in
    assert verify.verify_archive(z, study, level="content").ok


def test_choi_purge_registry_failure_reports_what_was_deleted(env, monkeypatch, caplog):
    created(env)
    assert bk(env, "remove", "S1") == 0
    gen = generations(env)[0]

    def broken(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(plugin, "save_registry", broken)
    with caplog.at_level(logging.INFO):
        assert bk(env, "purge", "S1", "--yes") == 1
    assert ("deleted %s" % gen) in caplog.text and "registry was not updated" in caplog.text
    assert not gen.exists()


def test_choi_unwrapped_archive_and_vanishing_raw(env, monkeypatch):
    study = make(env)
    plain = env.tmp / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:                   # no <key>/ wrapper
        for p in sorted(study.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(study).as_posix())
    assert verify.verify_archive(plain, study, level="content").ok
    real = verify.file_crc32

    def vanish(path, *a, **k):
        raise FileNotFoundError(path)

    monkeypatch.setattr(verify, "file_crc32", vanish)
    r = verify.verify_archive(plain, study, level="content")
    assert r.reasons == ["raw_changed"] and not r.ok
    monkeypatch.setattr(verify, "file_crc32", real)


def test_status_shows_verify_level_and_time(env, caplog):
    created(env)
    assert bk(env, "status", "--scan") == 0
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert bk(env, "status") == 0
    assert "VERIFY" in caplog.text and "content:OK" in caplog.text


# --- T1 same-size content change -------------------------------------------

def test_t1_same_size_change_fails_content_passes_crc(env, caplog):
    study, z = created(env)
    F.flip_byte_same_size(study / TWO_D, 5)
    arc_b, raw_b = sha(z), state(env.raw)
    with caplog.at_level(logging.INFO):
        assert bk(env, "verify", "S1", "--level", "content") == 1
    assert "content_mismatch" in caplog.text and TWO_D in caplog.text
    assert bk(env, "verify", "S1") == 0                      # crc: the archive itself is fine
    assert registry(env)["datasets"]["S1"]["verify"]["level"] == "crc"
    assert sha(z) == arc_b and state(env.raw) == raw_b


# --- T2 broken zip -------------------------------------------------------------

def test_t2a_member_data_damage_fails_crc(env, caplog):
    study, z = created(env)
    D.set_stored_crc(z, "S1/" + TWO_D, 0x1234)
    before = sha(z)
    with caplog.at_level(logging.INFO):
        assert bk(env, "verify", "S1") == 1
    assert "crc_mismatch" in caplog.text
    assert sha(z) == before


def test_t2b_truncated_zip_corrupt_then_repair(env, caplog):
    study, z = created(env)
    F.truncate_file(z, z.stat().st_size // 2)
    bad = z.read_bytes()
    raw_b = state(env.raw)
    with caplog.at_level(logging.INFO):
        assert bk(env, "verify", "S1") == 1
    assert "unreadable_zip" in caplog.text
    assert bk(env, "status", "--scan") == 0
    assert registry(env)["datasets"]["S1"]["status"] == "CORRUPT"
    assert bk(env, "repair", "S1") == 0
    assert bk(env, "verify", "S1", "--level", "content") == 0
    gens = generations(env)
    assert len(gens) == 1 and (gens[0] / "S1.zip").read_bytes() == bad
    assert state(env.raw) == raw_b
    assert not layout.journal_path(env.arc, "S1").exists()


# --- T3 interrupted repair ------------------------------------------------------

class Stop(Exception):
    pass


@pytest.mark.parametrize("stop_at", list(actions.REPAIR_STEPS) + ["writing_partial"])
def test_t3_interrupted_repair_keeps_a_whole_archive_and_resumes(env, monkeypatch, caplog, stop_at):
    study, z = created(env, scans=(1, 2))
    old = z.read_bytes()
    F.make_study(env.raw, "S1", scans=(3,))                 # raw gained scan 3 -> new archive differs
    raw_b = state(env.raw)

    if stop_at == "writing_partial":
        real_write = zipfile.ZipFile.write
        n = {"c": 0}

        def failing(self, *a, **k):
            n["c"] += 1
            if n["c"] > 2:
                raise Stop("interrupted while writing")
            return real_write(self, *a, **k)

        monkeypatch.setattr(zipfile.ZipFile, "write", failing)
    else:
        def hook(op, key, step):
            if step == stop_at:
                raise Stop("stopped after " + step)

        monkeypatch.setattr(actions, "_step_hook", hook)

    assert bk(env, "repair", "S1") == 1
    monkeypatch.undo()
    monkeypatch.setenv("BRKRAW_CONFIG_HOME", str(env.cfg))
    monkeypatch.setattr(actions, "_step_hook", None)

    # the archive path always holds a whole archive: the old one, or the verified new one
    assert z.is_file()
    with zipfile.ZipFile(z) as zf:
        assert zf.testzip() is None
    replaced = stop_at in ("replaced", "registry_updated")
    if replaced:
        assert verify.verify_archive(z, study, level="content").ok
    else:
        assert z.read_bytes() == old
    j = layout.read_journal(env.arc, "S1")
    assert j is not None and j["op"] == "repair"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert bk(env, "status", "--issues") == 0
    assert "unfinished:repair:" in caplog.text and "UNFINISHED" in caplog.text
    assert state(env.raw) == raw_b

    assert bk(env, "repair", "S1") == 0                   # rerun finishes the work
    assert not layout.journal_path(env.arc, "S1").exists()
    assert not layout.partial_path(env.arc, "S1").exists()
    assert verify.verify_archive(z, study, level="content").ok
    gens = generations(env)
    assert len(gens) == 1 and (gens[0] / "S1.zip").read_bytes() == old
    assert state(env.raw) == raw_b


# --- T4 raw missing ---------------------------------------------------------------

def test_t4_raw_missing(env, caplog):
    s1, z1 = created(env, "S1")
    s2, z2 = created(env, "S2")
    shutil.rmtree(s1)                                        # synthetic raw made by this test
    shutil.rmtree(s2)
    F.truncate_file(z1, 100)
    b1 = z1.read_bytes()
    with caplog.at_level(logging.INFO):
        assert bk(env, "repair", "S1") == 2
    assert "repair needs raw" in caplog.text
    assert z1.read_bytes() == b1 and not generations(env, "S1")
    assert bk(env, "verify", "S2") == 0                      # crc works without raw
    assert bk(env, "verify", "S2", "--level", "content") == 2
    b2 = sha(z2)
    assert bk(env, "remove", "S2") == 0                      # only moves to the trash
    assert not z2.exists()
    gens = generations(env, "S2")
    assert len(gens) == 1 and sha(gens[0] / "S2.zip") == b2


# --- T5 wrong target ---------------------------------------------------------------

def test_t5_unknown_names_change_nothing(env):
    created(env)
    before = state(env.tmp)
    assert bk(env, "repair", "NOPE") == 2
    assert bk(env, "remove", "NOPE") == 2
    assert bk(env, "purge", "S1", "--yes") == 2              # nothing in the trash for S1
    assert bk(env, "verify", "NOPE") == 2
    assert bk(env, "create", "NOPE") == 2
    assert bk(env, "repair") == 2                            # no all-targets default
    assert bk(env, "remove") == 2
    assert bk(env, "purge", "--yes") == 2
    assert state(env.tmp) == before


def test_t5_two_archives_for_one_key(env, caplog):
    study, z = created(env)
    F.zip_study(study, env.arc / "S1.PvDatasets")
    assert bk(env, "status", "--scan") == 0
    entry = registry(env)["datasets"]["S1"]
    assert entry["status"] == "DUPLICATE" and "duplicate_archive" in entry["issues"]
    before = state(env.tmp)
    with caplog.at_level(logging.INFO):
        assert bk(env, "create", "S1") == 2
        assert bk(env, "repair", "S1") == 2
        assert bk(env, "remove", "S1") == 2
        assert bk(env, "verify", "S1") == 2
    assert "pass --path" in caplog.text
    assert state(env.tmp) == before
    pv = sha(env.arc / "S1.PvDatasets")
    assert bk(env, "repair", "S1", "--path", "S1.zip") == 0
    assert sha(env.arc / "S1.PvDatasets") == pv


# --- T6 removed commands -------------------------------------------------------------

@pytest.mark.parametrize("old,new", [
    ("info", "status"), ("registry", "status"), ("scan", "status --scan"),
    ("review", "status --scan --issues"), ("run", "create"),
])
def test_t6_removed_commands(env, capsys, old, new):
    make(env)
    before = state(env.tmp)
    argv = ["backup", old, str(env.raw), str(env.arc), "--delete-raw", "--yes", "--rebuild"]
    assert main(argv) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "0.2.0" in err[0] and ("`brkraw backup %s`" % new) in err[0]
    assert main(["backup", old]) == 2
    assert state(env.tmp) == before


def test_t6_delete_raw_option_is_gone(env):
    make(env)
    with pytest.raises(SystemExit) as exc:
        bk(env, "create", "--delete-raw", "--yes")
    assert exc.value.code == 2


# --- T7 create with an existing archive ------------------------------------------------

def test_t7_create_skips_existing_archive(env, caplog):
    study, z = created(env)
    F.flip_byte_same_size(study / TWO_D, 3)                   # even if raw changed
    st = z.stat()
    b = sha(z)
    with caplog.at_level(logging.INFO):
        assert bk(env, "create", "S1") == 0
        assert bk(env, "create") == 0
    assert "skipped: archive exists" in caplog.text
    assert sha(z) == b and z.stat().st_mtime_ns == st.st_mtime_ns


# --- T8 multi-step failures ---------------------------------------------------------------

def test_t8_mixed_names_stop_before_any_change(env):
    created(env, "S1")
    before = state(env.tmp)
    assert bk(env, "remove", "S1", "NOPE") == 2
    assert bk(env, "repair", "S1", "../S1") == 2
    assert state(env.tmp) == before


def test_t8_remove_registry_failure_keeps_journal_and_stops(env, monkeypatch, caplog):
    _, z1 = created(env, "S1")
    _, z2 = created(env, "S2")
    b2 = sha(z2)

    def broken(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(plugin, "save_registry", broken)
    with caplog.at_level(logging.INFO):
        assert bk(env, "remove", "S1", "S2") == 1
    assert "Not started: S2" in caplog.text
    monkeypatch.setattr(plugin, "save_registry", __import__("brkraw_backup.core", fromlist=["x"]).save_registry)
    j = layout.read_journal(env.arc, "S1")
    assert j["op"] == "remove" and j["step"] == "moved"
    assert not z1.exists() and len(generations(env, "S1")) == 1
    assert sha(z2) == b2 and layout.read_journal(env.arc, "S2") is None
    assert bk(env, "repair", "S1") == 2                       # other op refused while unfinished
    assert bk(env, "remove", "S1") == 0                       # rerun finishes
    assert layout.read_journal(env.arc, "S1") is None
    assert "removed_at" in registry(env)["datasets"]["S1"]


def test_t8_purge_stops_midway_and_reports(env, monkeypatch, caplog):
    created(env)
    assert bk(env, "repair", "S1") == 0
    assert bk(env, "repair", "S1") == 0
    gens = generations(env)
    assert len(gens) == 2
    real = shutil.rmtree
    n = {"c": 0}

    def flaky(path, *a, **k):
        n["c"] += 1
        if n["c"] == 2:
            raise OSError("busy (simulated)")
        return real(path, *a, **k)

    monkeypatch.setattr(actions.shutil, "rmtree", flaky)
    with caplog.at_level(logging.INFO):
        assert bk(env, "purge", "S1", "--yes") == 1
    assert ("deleted %s" % gens[0]) in caplog.text and "stopped" in caplog.text
    assert not gens[0].exists() and gens[1].exists()


def test_purge_needs_confirmation(env, monkeypatch):
    created(env)
    assert bk(env, "remove", "S1") == 0
    before = state(env.tmp)
    assert bk(env, "purge", "S1") == 2                         # stdin is not a terminal in tests
    assert bk(env, "purge", "S1", "--dry-run") == 0
    assert state(env.tmp) == before
    monkeypatch.setattr(plugin.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert bk(env, "purge", "S1") == 2
    assert state(env.tmp) == before
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert bk(env, "purge", "S1") == 0
    assert not layout.trash_key_dir(env.arc, "S1").exists()


# --- T9 target paths ---------------------------------------------------------------------

def test_t9_only_the_expected_paths_change(env):
    created(env, "S1")
    created(env, "S2")
    assert bk(env, "repair", "S2") == 0                       # S2 has trash too
    F.make_study(env.raw, "S1", scans=(2,))                   # so the rebuilt S1.zip differs
    before = state(env.tmp)
    assert bk(env, "repair", "S1") == 0
    diff = changed(before, state(env.tmp))
    trash = ".brkraw-backup-trash/S1/"
    assert all(
        d in ("archive/S1.zip", "archive/.brkraw-backup-registry.json", "archive/.brkraw-backup-trash/S1")
        or d.startswith("archive/" + trash)
        for d in diff
    ), diff
    assert "archive/S1.zip" in diff
    before = state(env.tmp)
    assert bk(env, "purge", "S1", "--yes") == 0
    diff = changed(before, state(env.tmp))
    assert all(d.startswith("archive/.brkraw-backup-trash/S1") or d == "archive/.brkraw-backup-registry.json"
               for d in diff), diff
    assert generations(env, "S2")                             # the other key's trash stays


def test_t9_links_and_dotdot_are_refused(env, tmp_path):
    created(env, "S1")
    outside = tmp_path / "outside"
    outside.mkdir()
    real = F.zip_study(F.make_study(outside, "S3"), outside / "S3.zip")
    (env.arc / "S3.zip").symlink_to(real)
    F.make_study(env.raw, "S3")
    before = state(env.tmp)
    assert bk(env, "remove", "S3") == 2
    assert bk(env, "repair", "S3") == 2
    assert bk(env, "remove", "..") == 2
    assert bk(env, "remove", ".brkraw-backup-trash") == 2
    assert state(env.tmp) == before
    # a trash folder that is a link to somewhere else
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (env.arc / ".brkraw-backup-trash").symlink_to(elsewhere, target_is_directory=True)
    before = state(env.tmp)
    assert bk(env, "remove", "S1") == 2
    assert bk(env, "repair", "S1") == 2
    assert state(env.tmp) == before and list(elsewhere.iterdir()) == []


# --- T10 kinds of damage and content details -------------------------------------------------

def test_t10_reasons_are_separate(env):
    study, z = created(env, scans=(1, 2))
    cases = {}
    for name, hurt in (
        ("toc", lambda p: D.corrupt_central_directory(p)),
        ("crc", lambda p: D.set_stored_crc(p, "S1/" + TWO_D, 7)),
        ("data", lambda p: F.corrupt_zip_member_data(p, "S1/" + TWO_D)),
    ):
        copy = env.tmp / ("%s.zip" % name)
        shutil.copyfile(z, copy)
        hurt(copy)
        before = sha(copy)
        cases[name] = verify.verify_archive(copy, level="crc")
        assert sha(copy) == before
    assert cases["toc"].reasons == ["unreadable_zip"]
    assert cases["crc"].reasons == ["crc_mismatch"]
    assert cases["data"].reasons == ["bad_data"]
    assert cases["data"].details["bad_data"] == [TWO_D]


def test_t10_content_details(env, monkeypatch):
    study, z = created(env)
    raw_b = state(env.raw)
    arc_b = sha(z)
    # extra raw file -> missing in archive
    (study / "extra.txt").write_bytes(b"x")
    r = verify.verify_archive(z, study, level="content")
    assert "missing_files" in r.reasons and r.details["missing_files"] == ["extra.txt"]
    (study / "extra.txt").unlink()                            # file made by this test
    # duplicate member and extra archive member
    dup = env.tmp / "dup.zip"
    shutil.copyfile(z, dup)
    D.add_duplicate_member(dup, "S1/1/method", b"other")
    D.add_duplicate_member(dup, "S1/zzz", b"more")
    r = verify.verify_archive(dup, study, level="content")
    assert "duplicate_member" in r.reasons and "extra_files" in r.reasons
    # raw missing -> refused
    assert verify.verify_archive(z, env.raw / "none", level="content").reasons == ["raw_missing"]
    # raw changes while being verified
    real = verify.file_crc32
    n = {"c": 0}

    def touch(path, *a, **k):
        n["c"] += 1
        if n["c"] == 2:
            p = study / "1/acqp"
            p.write_bytes(p.read_bytes() + b"!")
        return real(path, *a, **k)

    monkeypatch.setattr(verify, "file_crc32", touch)
    r = verify.verify_archive(z, study, level="content")
    assert "raw_changed" in r.reasons
    assert sha(z) == arc_b
    assert changed(raw_b, state(env.raw)) == ["S1/1/acqp"]      # only the test's own edit


def test_list_level_checks_names_and_sizes(env):
    study, z = created(env)
    assert verify.verify_archive(z, study, level="list").ok
    (study / "1/method").write_bytes(b"a different size\n")
    r = verify.verify_archive(z, study, level="list")
    assert r.reasons == ["size_mismatch"]


# --- no code path deletes raw ---------------------------------------------------------------

REMOVING = {"rmtree", "unlink", "remove", "rmdir", "removedirs", "replace", "rename"}
ALLOWED = {
    ("actions.py", "create_one", "unlink"),        # its own .partial after a failed verify
    ("actions.py", "repair_one", "unlink"),        # its own .partial after a failed verify
    ("actions.py", "repair_one", "replace"),       # verified .partial -> archive path
    ("actions.py", "_place_new", "unlink"),        # .partial after it was linked in place
    ("actions.py", "_place_new", "replace"),       # .partial -> new archive (no dest yet)
    ("actions.py", "remove_one", "replace"),       # archive -> trash
    ("actions.py", "purge_one", "rmtree"),         # trash generation
    ("actions.py", "purge_one", "rmdir"),          # empty trash key folder
    ("layout.py", "write_journal", "replace"),     # journal tmp -> journal
    ("layout.py", "drop_journal", "unlink"),       # journal
    ("layout.py", "create_new", "unlink"),         # an existing name before an exclusive create
    ("core.py", "save_registry", "replace"),       # registry tmp -> registry
}
WRITE_MODES = set("wax+")


def test_every_file_write_goes_through_create_new():
    """No builtin/Path open() for writing: new files are made only by layout.create_new
    (exclusive create, never through a link), so no write can land in a raw file."""
    found = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            is_open = (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open")
            if not is_open:
                continue
            modes = [a.value for a in list(node.args) + [k.value for k in node.keywords if k.arg == "mode"]
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if any(WRITE_MODES & set(m) for m in modes):
                found.append((path.name, node.lineno))
    assert found == []


def test_no_code_path_deletes_raw():
    found = set()
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                attr, owner = node.func.attr, node.func.value
                if attr not in REMOVING:
                    continue
                owner_name = owner.id if isinstance(owner, ast.Name) else None
                if attr == "replace" and owner_name == "dataclasses":
                    continue                                   # dataclasses.replace: not a file call
                if attr == "replace" and owner_name != "os" and len(node.args) != 1:
                    continue                                   # str.replace(a, b): not a file call
                found.add((path.name, fn.name, attr))
    assert found == ALLOWED
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.py"))
    assert "delete_raw" not in text and "maybe_delete_raw" not in text


def test_readme_matches_the_command_set():
    from brkraw.cli.main import build_parser
    _, subparsers = build_parser()
    backup = subparsers.choices["backup"]
    sub = next(a for a in backup._actions if a.__class__.__name__ == "_SubParsersAction")
    registered = set(sub.choices)
    main_cmds = {"status", "create", "verify", "repair", "remove", "purge", "init", "migrate", "about"}
    assert registered == main_cmds | set(plugin.REMOVED_COMMANDS)
    readme = (SRC.parents[1] / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Commands", 1)[1].split("###", 1)[0]
    for name in main_cmds:
        assert ("`%s" % name) in table, name
    changes = readme.split("## Changes in 0.2.0", 1)[1]
    for name in plugin.REMOVED_COMMANDS:
        assert ("`%s`" % name) in changes, name
    assert "never deletes a raw folder" in readme


def test_help_lists_the_new_commands(capsys):
    with pytest.raises(SystemExit):
        main(["backup", "--help"])
    out = capsys.readouterr().out
    for name in ("status", "create", "verify", "repair", "remove", "purge", "init", "migrate", "about"):
        assert name in out
    assert "never deletes raw" in out
