"""Smoke tests: brkraw's CLI finds the installed plugin and runs it; `--root`
means the brkraw config folder, as for brkraw's own commands."""

import logging

from brkraw.cli.main import build_parser, main


def test_backup_is_a_brkraw_command():
    _, subparsers = build_parser()
    assert "backup" in subparsers.choices


def test_about_runs_with_a_config_root(tmp_path, caplog):
    root = tmp_path / "config"
    with caplog.at_level(logging.INFO):  # about reports through the log
        assert main(["backup", "about", "--root", str(root)]) == 0
    assert str(root) in caplog.text
