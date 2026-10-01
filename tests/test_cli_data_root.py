"""CLI --data-root 默认值（env 优先）与「快照未注册」报错带数据根提示。"""
import os
from pathlib import Path

import pytest

from cold_manifest.cli import _build_parser, main


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CLDM_DATA_ROOT", raising=False)


def test_default_data_root_falls_back_to_dot_slash_data():
    args = _build_parser().parse_args(["hash", "VOL/20260101T000000Z"])
    assert args.data_root == str(Path("./data").resolve())


def test_default_data_root_prefers_env(monkeypatch):
    monkeypatch.setenv("CLDM_DATA_ROOT", "/tmp/env-root")
    args = _build_parser().parse_args(["duplicates", "VOL/20260101T000000Z"])
    assert args.data_root == "/tmp/env-root"


def test_explicit_data_root_beats_env(monkeypatch):
    monkeypatch.setenv("CLDM_DATA_ROOT", "/tmp/env-root")
    args = _build_parser().parse_args(
        ["duplicates", "VOL/X", "--data-root", "/tmp/explicit"])
    assert args.data_root == "/tmp/explicit"


def test_hash_unregistered_mentions_data_root(tmp_path, capsys):
    rc = main(["hash", "NOPE/20260101T000000Z", "--data-root", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 2
    assert "快照未注册" in out.err
    assert str(tmp_path.resolve()) in out.err
    assert "--data-root" in out.err
