"""The launch flow with fake clients (no uv)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from tgzr.edge.home_manager import ClientSpec, Home, LastLaunch, OrgInfo, OrgUnreachable, Venv, pid_alive

from tgzr.edge.launcher import core
from tgzr.edge.launcher.core import (
    ClientAlreadyRunning,
    LaunchError,
    SpecOverrides,
    Target,
    default_target,
    launch,
    select_chain,
)

from .conftest import ORG, fake_venv


def test_launch_current_and_record_launcher(home, chain, clients):
    fake_venv(chain, 1)
    r = clients.add(launch(home, chain, ready_timeout=20).pid)
    assert pid_alive(r)
    assert chain.current_number() == 1 and chain.read_instance().pid == r
    assert home.launcher_command[-2:] == ["-m", "tgzr.edge.launcher"]
    assert home.last_launch == LastLaunch(ORG, "fake_client")
    log = (chain.logs_dir / "client-1.log").read_text()
    assert f"org={ORG}" in log and "tgzr.edge.launcher" in log  # TGZR_ORG and TGZR_LAUNCHER were set


def test_client_env_and_the_handed_over_link(home, chain, clients):
    fake_venv(chain, 1)
    r = launch(home, chain, ready_timeout=20, client_env={"TGZR_CLIENT_NO_BROWSER": "1"})
    clients.add(r.pid)
    assert r.url == "http://127.0.0.1:1/?launch=fake"


def test_no_link_by_default(home, chain, clients):
    fake_venv(chain, 1)
    r = launch(home, chain, ready_timeout=20)
    clients.add(r.pid)
    assert r.url is None


def test_first_launch_builds_venv_1(home, chain, clients, monkeypatch):
    built = []

    def fake_build(self, steps, *, on_event=None):
        built.append(steps)
        fake_venv(self, 1)
        return Venv(1, self.path / "1")

    monkeypatch.setattr(type(chain), "build", fake_build)
    clients.add(launch(home, chain, ready_timeout=20).pid)
    assert built[0][0].requirements == ("fake_client",)  # the client spec only, no plugins
    assert chain.current_number() == 1


def test_already_running(home, chain, clients):
    fake_venv(chain, 1)
    clients.add(launch(home, chain, ready_timeout=20).pid)
    with pytest.raises(ClientAlreadyRunning, match="already running"):
        launch(home, chain, ready_timeout=20)
    assert chain.names() == ["1"]


def test_client_exiting_3_is_already_running_not_failing(home, chain, monkeypatch):
    fake_venv(chain, 1)
    monkeypatch.setattr(type(chain), "is_running", lambda self: False)  # a race past the launcher's own check
    with chain.instance_lock(), pytest.raises(ClientAlreadyRunning):
        launch(home, chain, ready_timeout=20)
    assert chain.names() == ["1"]


def test_another_client_of_the_same_org_runs_alongside(home, chain, clients):
    fake_venv(chain, 1)
    clients.add(launch(home, chain, ready_timeout=20).pid)
    other = home.org(ORG).client("fake-client-two")
    other.save_spec(ClientSpec("fake_client"))
    fake_venv(other, 1)
    clients.add(launch(home, other, ready_timeout=20).pid)
    assert chain.is_running() and other.is_running()


def test_broken_venv_falls_back_to_the_previous_one(home, chain, clients):
    fake_venv(chain, 1)
    fake_venv(chain, 2, "crash")
    r = launch(home, chain, switch_to=2, ready_timeout=20)
    clients.add(r.pid)
    assert r.fell_back and r.venv.number == 1
    assert chain.names() == ["1", "2_failing"] and chain.current_number() == 1
    assert chain.next_number() == 3


def test_never_ready_is_killed_at_the_timeout(home, chain, clients):
    fake_venv(chain, 1)
    fake_venv(chain, 2, "hang")
    t = time.monotonic()
    r = launch(home, chain, switch_to=2, ready_timeout=1)
    clients.add(r.pid)
    assert time.monotonic() - t < 15
    assert r.venv.number == 1 and chain.names() == ["1", "2_failing"]


def test_no_previous_venv_is_an_explicit_error(home, chain):
    fake_venv(chain, 1, "crash")
    with pytest.raises(LaunchError, match="no previous working venv") as e:
        launch(home, chain, ready_timeout=20)
    assert e.value.log_path.name == "client-1.log"
    assert chain.names() == ["1_failing"]


def test_previous_failing_too(home, chain):
    fake_venv(chain, 1, "crash")
    fake_venv(chain, 2, "crash")
    with pytest.raises(LaunchError, match="previous venv failed too"):
        launch(home, chain, switch_to=2, ready_timeout=20)
    assert chain.names() == ["1", "2_failing"]


def test_exit_0_before_ready_is_success(home, chain):
    fake_venv(chain, 1, "exit0")
    r = launch(home, chain, ready_timeout=20)
    assert r.exited and chain.names() == ["1"]


def test_successful_start_prunes(home, chain, clients):
    for n in (1, 2, 3):
        fake_venv(chain, n)
    fake_venv(chain, "0_failing")
    clients.add(launch(home, chain, switch_to=3, ready_timeout=20).pid)
    assert chain.names() == ["0_failing", "2", "3"]


def test_select_chain_saves_and_overrides_the_spec(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = select_chain(home, ORG, "tgzr.edge.nice_client", SpecOverrides(version=">=0.1,<1", editable=("src",)))
    assert c.spec == ClientSpec("tgzr.edge.nice_client", ">=0.1,<1", editable=(str(tmp_path / "src"),))
    c = select_chain(home, ORG, None, SpecOverrides(extras=("x>=1",)))  # the only chain of the org
    assert c.spec.extras == ("x>=1",) and c.spec.version == ">=0.1,<1"
    c = select_chain(home, ORG, "tgzr.edge.nice_client", SpecOverrides())
    assert c.spec.extras == ("x>=1",)  # reused as saved
    select_chain(home, ORG, "other", SpecOverrides())
    with pytest.raises(LaunchError, match="give --client"):
        select_chain(home, ORG, None, SpecOverrides())


def test_headless_command_switch_to_and_wait_pid(home, chain, clients, tmp_path):
    fake_venv(chain, 1)
    fake_venv(chain, 2)
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    threading.Thread(target=old.wait, daemon=True).start()  # reap it: a zombie still answers kill(0)
    t = time.monotonic()
    res = subprocess.run(
        [sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--home-dir", str(home.path), "--org", ORG,
         "--client", "fake_client", "--switch-to", "2", "--wait-pid", str(old.pid), "--ready-timeout", "20"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert time.monotonic() - t >= 1.4 and old.poll() is not None  # waited for the old process
    assert f"waiting for process {old.pid}" in res.stderr
    clients.add(chain.read_instance().pid)
    assert chain.current_number() == 2
    again = subprocess.run(
        [sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--home-dir", str(home.path), "--org", ORG],
        capture_output=True, text=True, timeout=60,
    )
    assert again.returncode == 3 and "already running" in again.stderr


def test_headless_errors(tmp_path, home):
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "tgzr.edge.launcher", "--headless", *args],
                              capture_output=True, text=True, timeout=60)

    r = run("--home-dir", str(tmp_path / "nohome"), "--org", ORG, "--client", "x")
    assert r.returncode == 1 and "not a tgzr home" in r.stderr and not (tmp_path / "nohome").exists()
    r = run("--home-dir", str(home.path))
    assert r.returncode == 1 and "nothing to launch: give --org" in r.stderr
    r = run("--home-dir", str(home.path), "--manage")
    assert r.returncode == 1 and "give --headless or --manage, not both" in r.stderr
    r = run("--home-dir", str(tmp_path / "new"), "--create-home", "--org", ORG, "--client", "fake_client",
            "--find-links", str(tmp_path / "nowheels"), "--index-url", "https://invalid.invalid/simple")
    assert r.returncode == 1 and "created the tgzr home" in r.stderr and "could not build" in r.stderr
    assert json.loads((tmp_path / "new" / "tgzr_home.json").read_text())["layout"] == 1


def test_stop_a_running_client(home, chain, clients):
    fake_venv(chain, 1)
    r = launch(home, chain, ready_timeout=20)
    clients.add(r.pid)
    assert chain.is_running()
    assert chain.stop(timeout=5) is True
    assert not chain.is_running() and not pid_alive(r.pid)
    assert chain.stop() is False


@pytest.mark.parametrize(("flags", "native"), [((), "1"), (("--native",), "1"), (("--web",), "0")])
def test_headless_window_mode(home, chain, clients, flags, native):
    fake_venv(chain, 1)
    res = subprocess.run(
        [sys.executable, "-m", "tgzr.edge.launcher", "--headless", *flags, "--home-dir", str(home.path),
         "--org", ORG, "--client", "fake_client", "--ready-timeout", "20"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    clients.add(chain.read_instance().pid)
    assert f"native={native}" in (chain.logs_dir / "client-1.log").read_text()


def test_native_and_web_together_is_an_error(home):
    res = subprocess.run([sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--native", "--web",
                          "--home-dir", str(home.path)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 1 and "give --native or --web, not both" in res.stderr


def test_client_env():
    from tgzr.edge.launcher.core import client_env

    native = {"TGZR_CLIENT_NATIVE": "1", "TGZR_CLIENT_WEB": "0", "TGZR_CLIENT_RELOAD": "0"}
    assert client_env() == native
    assert client_env(native=True, hand_over_link=True) == native
    assert client_env(native=False, hand_over_link=True) == {"TGZR_CLIENT_NATIVE": "0", "TGZR_CLIENT_WEB": "1",
                                                              "TGZR_CLIENT_RELOAD": "0", "TGZR_CLIENT_NO_BROWSER": "1"}
    # --reload is passed on (always set: it also overrides a .env saying the opposite); it implies the browser.
    assert client_env(native=False, reload=True) == {"TGZR_CLIENT_NATIVE": "0", "TGZR_CLIENT_WEB": "1",
                                                      "TGZR_CLIENT_RELOAD": "1"}
    with pytest.raises(ValueError, match="implies --web"):
        client_env(native=True, reload=True)


def test_concurrent_launches_of_one_chain(home, chain, clients):
    """Two launchers at once: one starts the client, the other waits for the
    launch lock, then finds it running (no deleted ready file, no venv marked
    failing)."""
    fake_venv(chain, 1)
    results: dict[str, object] = {}

    def run(name: str) -> None:
        try:
            results[name] = launch(home, chain, ready_timeout=20)
        except Exception as e:  # noqa: BLE001
            results[name] = e

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    ok = [r for r in results.values() if not isinstance(r, Exception)]
    errors = [r for r in results.values() if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errors) == 1, results
    clients.add(ok[0].pid)
    assert isinstance(errors[0], ClientAlreadyRunning)
    assert chain.names() == ["1"] and chain.read_instance().pid == ok[0].pid


def test_launch_lock_timeout(home, chain):
    fake_venv(chain, 1)
    messages = []
    with chain.launch_lock():  # another launcher is busy with this chain
        with pytest.raises(LaunchError, match="in progress"):
            launch(home, chain, ready_timeout=20, lock_timeout=0.3, progress=messages.append)
    assert messages == [f"waiting for another launch of fake_client for {ORG}"]


def test_launcher_command_is_the_pyapp_executable(tmp_path, monkeypatch):
    from tgzr.edge.launcher.core import launcher_command

    exe = tmp_path / "tgzr-launcher"
    exe.write_text("")
    monkeypatch.setenv("PYAPP", str(exe))
    assert launcher_command() == [str(exe)]
    monkeypatch.setenv("PYAPP", "1")  # PyApp without PYAPP_PASS_LOCATION: not a path
    assert launcher_command() == [sys.executable, "-m", "tgzr.edge.launcher"]


def test_default_index_only_for_a_new_spec(home):
    from tgzr.edge.home_manager import ClientSpec as Spec

    default = SpecOverrides(default_index_urls=("https://default/simple",))
    new = select_chain(home, "https://new.test", "some_client", default)
    assert new.spec.index_urls == ("https://default/simple",)
    # A saved spec keeps its index when only a default is given...
    org = home.add_org("https://saved.test")
    org.client("some_client").save_spec(Spec("some_client", index_urls=("https://saved/simple",)))
    kept = select_chain(home, "https://saved.test", "some_client", default)
    assert kept.spec.index_urls == ("https://saved/simple",)
    # ...and an explicit index still overrides it.
    explicit = SpecOverrides(index_urls=("https://explicit/simple",), default_index_urls=("https://default/simple",))
    assert select_chain(home, "https://saved.test", "some_client", explicit).spec.index_urls == (
        "https://explicit/simple",)


def test_default_target(tmp_path, home):
    assert default_target(home) is None
    assert default_target(home, default_org=" https://community.test ", default_client="c") == Target(
        "https://community.test", "c")
    chain = home.add_org(ORG).client("fake_client")
    assert default_target(home, default_org="https://community.test") is None  # the home has an org: no default
    chain.save_spec(ClientSpec("fake_client"))
    home.last_launch = LastLaunch(ORG, "fake_client")
    assert default_target(home, default_org="https://community.test") == Target(ORG, "fake_client")
    assert default_target(home, client="other") == Target(ORG, "other")
    chain.org.remove_client("fake_client")  # the last launch is gone with its chain
    assert default_target(home) is None


def test_headless_launches_the_last_one(home, chain, clients):
    fake_venv(chain, 1)
    home.last_launch = LastLaunch(ORG, "fake_client")
    res = subprocess.run(
        [sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--home-dir", str(home.path),
         "--ready-timeout", "20"], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    clients.add(chain.read_instance().pid)


def test_a_new_org_is_asked_for_its_name(home, monkeypatch):
    asked: list[str] = []

    def info(url, timeout):
        asked.append(url)
        return OrgInfo(url, "Acme", "0.200")

    monkeypatch.setattr(core, "fetch_org_info", info)
    chain = select_chain(home, ORG, "fake_client", SpecOverrides())
    assert chain.org.name == "Acme" and home.org(ORG).name == "Acme"
    select_chain(home, ORG, "fake_client", SpecOverrides())
    assert asked == [ORG]  # only when the org is new

    def down(url, timeout):
        raise OrgUnreachable("down")

    monkeypatch.setattr(core, "fetch_org_info", down)
    assert select_chain(home, "https://down.test", "fake_client", SpecOverrides()).org.name == "https://down.test"


def test_direct_target(tmp_path, monkeypatch):
    from tgzr.edge.launcher.cli import direct_target
    from tgzr.edge.launcher.settings import LauncherSettings

    for k in list(os.environ):
        if k.startswith("TGZR_"):
            monkeypatch.delenv(k)
    new = tmp_path / "new"
    assert direct_target(LauncherSettings(home_dir=new)) is None and not new.exists()  # the page asks where
    assert direct_target(LauncherSettings(home_dir=new, default_org="https://community.test")) == Target(
        "https://community.test", "tgzr.edge.nice_client")
    assert Home.open(new).orgs() == []  # created, the org is added by the launch
    assert direct_target(LauncherSettings(home_dir=new, org=ORG, client="c")) == Target(ORG, "c")
    home = Home.open(new)
    home.add_org(ORG)
    assert direct_target(LauncherSettings(home_dir=new, default_org="https://community.test")) is None


def test_headless_no_browser_prints_the_link(home, chain, clients):
    fake_venv(chain, 1)
    res = subprocess.run(
        [sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--web", "--no-browser", "--home-dir",
         str(home.path), "--org", ORG, "--client", "fake_client", "--ready-timeout", "20"],
        capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    clients.add(chain.read_instance().pid)
    assert res.stdout == "http://127.0.0.1:1/?launch=fake\n"  # the link alone; progress is on stderr


def test_resolve_org(tmp_path, home, monkeypatch):
    from tgzr.edge.home_manager import AmbiguousOrg, UnknownOrg

    from tgzr.edge.launcher.cli import resolve_org
    from tgzr.edge.launcher.settings import LauncherSettings

    def resolved(org: str | None, home_dir=home.path) -> str | None:
        return resolve_org(LauncherSettings(home_dir=home_dir, org=org)).org

    home.add_org(ORG, "Acme")
    assert resolved(None) is None
    assert resolved("https://elsewhere.test") == "https://elsewhere.test"  # a URL is never looked up
    assert resolved("Acme") == ORG and resolved(" acme ") == ORG
    with pytest.raises(UnknownOrg, match=r"no org named 'Nope' .*\(known: Acme\); give its URL to add it"):
        resolved("Nope")
    home.add_org("https://acme2.test", "ACME")
    with pytest.raises(AmbiguousOrg, match="https://acme2.test"):
        resolved("acme")
    with pytest.raises(UnknownOrg, match="is not a tgzr home yet"):
        resolved("Acme", tmp_path / "nohome")
    assert not (tmp_path / "nohome").exists()


def test_headless_org_by_name(home, clients):
    chain = home.add_org(ORG, "Acme").client("fake_client")
    chain.save_spec(ClientSpec("fake_client"))
    fake_venv(chain, 1)

    def run(org: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tgzr.edge.launcher", "--headless", "--home-dir", str(home.path), "--org", org,
             "--ready-timeout", "20"], capture_output=True, text=True, timeout=60)

    res = run("Nope")
    assert res.returncode == 1 and "no org named 'Nope'" in res.stderr and "known: Acme" in res.stderr
    assert [o.url for o in home.orgs()] == [ORG]  # a name never adds an org
    res = run("acme")
    assert res.returncode == 0, res.stderr
    clients.add(chain.read_instance().pid)
    assert chain.is_running()
