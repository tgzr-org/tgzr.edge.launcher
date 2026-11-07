"""LauncherSettings: flag > env > closest .env > default; the home never from .env."""

from __future__ import annotations

from pathlib import Path

from tgzr.edge.launcher.settings import load_settings


def test_precedence(home, chain, monkeypatch):
    monkeypatch.setenv("TGZR_HOME", str(home.path))
    assert load_settings([]).ready_timeout == 60
    (home.path / ".env").write_text("TGZR_LAUNCHER_READY_TIMEOUT=11\nTGZR_LAUNCHER_ORG=https://from.env\n")
    s = load_settings([])
    assert s.ready_timeout == 11 and s.org == "https://from.env"
    chain.path.mkdir(parents=True, exist_ok=True)
    (chain.path / ".env").write_text("TGZR_LAUNCHER_READY_TIMEOUT=22\n")
    assert load_settings([], focus=chain.path).ready_timeout == 22  # the closest only
    assert load_settings([], focus=chain.path).org is None  # no merging with the home's .env
    monkeypatch.setenv("TGZR_LAUNCHER_READY_TIMEOUT", "33")
    assert load_settings([], focus=chain.path).ready_timeout == 33
    assert load_settings(["--ready-timeout", "44"], focus=chain.path).ready_timeout == 44


def test_home_dir(home, tmp_path, monkeypatch):
    assert load_settings([]).home_path == Path.home() / "tgzr_home"
    monkeypatch.setenv("TGZR_HOME", str(home.path))
    (home.path / ".env").write_text(f"TGZR_HOME={tmp_path / 'other'}\nTGZR_LAUNCHER_HOME_DIR=/x\n")
    assert load_settings([]).home_path == home.path  # never from .env
    assert load_settings(["--home-dir", str(tmp_path / "flag")]).home_path == tmp_path / "flag"


def test_repeatable_flags(home):
    s = load_settings(["--extra", "a>=1", "--extra", "b", "--editable", "/x", "--headless", "--switch-to", "2"])
    assert s.extra == ["a>=1", "b"] and s.editable == ["/x"] and s.headless and s.switch_to == 2


def test_negative_sounding_flags(home):
    assert load_settings(["--no-browser", "--create-home"]).no_browser
    assert not load_settings([]).no_browser
    assert load_settings([]).use_native and load_settings(["--native"]).use_native  # native by default
    assert not load_settings(["--web"]).use_native
