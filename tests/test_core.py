"""The launch flow with fake clients (no uv)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

from tgzr.edge.home_manager import ClientSpec, Venv, pid_alive

from tgzr.edge.launcher.core import ClientAlreadyRunning, LaunchError, SpecOverrides, launch, select_chain

from .conftest import ORG, fake_venv


def test_launch_current_and_record_launcher(home, chain, clients):
    fake_venv(chain, 1)
    r = clients.add(launch(home, chain, ready_timeout=20).pid)
    assert pid_alive(r)
    assert chain.current_number() == 1 and chain.read_instance().pid == r
    assert home.launcher_command[-2:] == ["-m", "tgzr.edge.launcher"]
    assert chain.org.touch is not None and home.org(ORG).last_used
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

    def fake_build(self, steps):
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
    assert r.returncode == 1 and "--org is required" in r.stderr
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

    assert client_env() == {"TGZR_CLIENT_NATIVE": "1", "TGZR_CLIENT_WEB": "0"}
    assert client_env(native=True, hand_over_link=True) == {"TGZR_CLIENT_NATIVE": "1", "TGZR_CLIENT_WEB": "0"}
    assert client_env(native=False, hand_over_link=True) == {"TGZR_CLIENT_NATIVE": "0", "TGZR_CLIENT_WEB": "1",
                                                              "TGZR_CLIENT_NO_BROWSER": "1"}


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
