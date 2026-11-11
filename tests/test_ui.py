"""The launcher's page in process (NiceGUI user simulation); launches are faked."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from nicegui.testing.general import nicegui_reset_globals
from nicegui.testing.user import User

from tgzr.edge.home_manager import ClientSpec, Home, Venv

from tgzr.edge.home_manager import NoLink

from tgzr.edge.launcher.core import ClientAlreadyRunning, LaunchError, LaunchResult
from tgzr.edge.launcher.guard import LaunchTokens
from tgzr.edge.launcher.settings import LauncherSettings
from tgzr.edge.launcher.ui import LauncherState, create_app

COOKIE = "tgzr.edge.launcher_test"
ORG = "https://org.test"


class FakeLaunch:
    def __init__(self, error: LaunchError | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.client_envs: list[dict] = []
        self.error = error

    def __call__(self, home, chain, *, switch_to, ready_timeout, progress, client_env):
        progress(f"starting {chain.slug}")
        self.calls.append((chain.org.url, chain.slug))
        self.client_envs.append(client_env)
        if self.error is not None:
            raise self.error
        return LaunchResult(Venv(1, chain.path / "1"), 4242, exited=False, fell_back=False,
                            url="http://127.0.0.1:5555/?launch=tok")


async def open_form(user: User) -> None:
    """Open the "Add a connection" dialog."""
    user.find(marker="add-connection").click()
    await user.should_see(marker="org-url")


def chain_marker(home: Home) -> str:
    chain = home.org(ORG).client("tgzr.edge.nice_client")
    return f"chain-{chain.org.slug}-{chain.slug}"


@asynccontextmanager
async def launcher_env(tmp_path, home_dir, launch_fn, *, web: bool = True, reload: bool = False):
    os.environ["NICEGUI_USER_SIMULATION"] = "true"
    done: list[int] = []
    st = LauncherState(settings=LauncherSettings(home_dir=home_dir, web=web, reload=reload), argv=None, on_done=lambda: done.append(1),
                       launch_fn=launch_fn)
    st.navigated = []  # type: ignore[attr-defined]
    st.navigate = st.navigated.append  # type: ignore[attr-defined]
    try:
        with nicegui_reset_globals():
            tokens = LaunchTokens()
            app = create_app(st, launch_tokens=tokens, allowed_hosts={"test"}, cookie_name=COOKIE)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
                    assert (await http.get("/")).status_code == 403  # no launch link
                    r = await http.get("/", params={"launch": tokens.issue()})
                    http.cookies.set(COOKIE, r.cookies[COOKIE])
                    yield User(http), st, done
    finally:
        os.environ.pop("NICEGUI_USER_SIMULATION", None)


async def test_creates_the_home(tmp_path):
    async with launcher_env(tmp_path, tmp_path / "h", FakeLaunch()) as (user, st, done):
        await user.open("/")
        await user.should_see(marker="create-home-title")
        user.find(marker="home-path").clear().type(str(tmp_path / "chosen"))
        user.find(marker="create-home").click()
        await user.should_see(marker="add-connection", retries=20)  # a fresh home can add a connection
        assert (tmp_path / "chosen" / "tgzr_home.json").is_file()
        assert st.home_path == tmp_path / "chosen"


async def test_launch_from_the_form(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch()
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        await user.open("/")
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        user.find(marker="client-version").type(">=0.1,<1")
        user.find(marker="launch").click()
        await user.should_see(marker="launched", retries=50)
        assert fake.calls == [(ORG, "tgzr.edge.nice_client")] and done == [1]
        assert fake.client_envs == [{"TGZR_CLIENT_NATIVE": "0", "TGZR_CLIENT_WEB": "1", "TGZR_CLIENT_RELOAD": "0",
                                     "TGZR_CLIENT_NO_BROWSER": "1"}]  # a web client hands its link back
        assert st.navigated == ["http://127.0.0.1:5555/?launch=tok"]  # this tab goes there
        assert home.org(ORG).client("tgzr.edge.nice_client").spec.version == ">=0.1,<1"


async def test_form_needs_an_org(tmp_path):
    home = Home.create(tmp_path / "h")
    async with launcher_env(tmp_path, home.path, FakeLaunch()) as (user, st, done):
        await user.open("/")
        await open_form(user)
        user.find(marker="launch").click()
        await user.should_see("The org URL and the client are required.")
        await user.should_see(marker="org-url")  # the dialog stays open
        user.find(marker="add-cancel").click()
        await user.should_not_see(marker="org-url")


async def test_known_chains_launch_and_errors_show_the_log(tmp_path):
    home = Home.create(tmp_path / "h")
    chain = home.add_org(ORG, name="Acme").client("tgzr.edge.nice_client")
    chain.save_spec(ClientSpec("tgzr.edge.nice_client"))
    fake = FakeLaunch(LaunchError("the client exited with code 2", chain.logs_dir / "client-1.log"))
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        await user.open("/")
        await user.should_see("Acme")
        user.find(marker=f"launch-{chain.org.slug}-{chain.slug}").click()
        await user.should_see(marker="launch-error", retries=50)
        await user.should_see(marker="log-path")
        assert fake.calls == [(ORG, "tgzr.edge.nice_client")] and done == [] and st.navigated == []


async def test_already_running_opens_the_running_client(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch(ClientAlreadyRunning("tgzr.edge.nice_client is already running for https://org.test"))
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        asked = []
        st.link_fn = lambda chain: asked.append(chain.slug) or "http://127.0.0.1:6666/?launch=again"
        await user.open("/")
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        user.find(marker="launch").click()
        await user.should_see("is running for https://org.test: opening it", retries=50)
        await user.should_not_see(marker="launch-error")
        assert asked == ["tgzr.edge.nice_client"]
        assert st.navigated == ["http://127.0.0.1:6666/?launch=again"] and done == [1]
        assert st.opened == ["http://127.0.0.1:6666/?launch=again"]


async def test_already_running_without_a_link_says_why(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch(ClientAlreadyRunning("tgzr.edge.nice_client is already running for https://org.test"))
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        def no_link(chain):
            raise NoLink("connection refused")

        st.link_fn = no_link
        await user.open("/")
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        user.find(marker="launch").click()
        await user.should_see(marker="launch-error", retries=50)
        await user.should_see("but it gives no link: connection refused")
        assert st.navigated == [] and done == []


async def test_native_checkbox(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch()
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        await user.open("/")
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        user.find(marker="native").click()
        user.find(marker="launch").click()
        await user.should_see(marker="launched", retries=50)
        assert fake.client_envs == [{"TGZR_CLIENT_NATIVE": "1", "TGZR_CLIENT_WEB": "0", "TGZR_CLIENT_RELOAD": "0"}]


async def test_reload_is_passed_on_to_the_client(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch()
    async with launcher_env(tmp_path, home.path, fake, web=False, reload=True) as (user, st, done):
        await user.open("/")
        native = user.find(marker="native").elements.pop()
        assert native.value is False and not native.enabled  # --reload implies the browser
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        user.find(marker="launch").click()
        await user.should_see(marker="launched", retries=50)
        assert fake.client_envs == [{"TGZR_CLIENT_NATIVE": "0", "TGZR_CLIENT_WEB": "1", "TGZR_CLIENT_RELOAD": "1",
                                     "TGZR_CLIENT_NO_BROWSER": "1"}]


async def test_native_launcher_ticks_native_by_default(tmp_path):
    home = Home.create(tmp_path / "h")
    async with launcher_env(tmp_path, home.path, FakeLaunch(), web=False) as (user, st, done):
        await user.open("/")
        assert user.find(marker="native").elements.pop().value is True


async def test_running_known_client_shows_browse_and_stop(tmp_path):
    home = Home.create(tmp_path / "h")
    chain = home.add_org(ORG, name="Acme").client("tgzr.edge.nice_client")
    chain.save_spec(ClientSpec("tgzr.edge.nice_client"))
    key = f"{chain.org.slug}-{chain.slug}"
    held = chain.instance_lock()  # the client is running
    try:
        async with launcher_env(tmp_path, home.path, FakeLaunch()) as (user, st, done):
            stops = []

            def fake_stop(c):
                stops.append(c.slug)
                held.release()
                return True

            st.stop_fn = fake_stop
            st.link_fn = lambda c: "http://127.0.0.1:7777/?launch=b"
            await user.open("/")
            await user.should_see(marker=f"running-{key}")
            await user.should_see(marker=f"browse-{key}")
            await user.should_not_see(marker=f"launch-{key}")
            user.find(marker=f"stop-{key}").click()
            await user.should_see("Stop tgzr.edge.nice_client for Acme?", retries=20)
            user.find(marker="stop-cancel").click()  # changed my mind
            await user.should_not_see(marker="stop-question", retries=20)
            assert stops == [] and chain.is_running()
            user.find(marker=f"stop-{key}").click()
            await user.should_see(marker="stop-question", retries=20)
            user.find(marker="stop-confirm").click()
            await user.should_see(marker="stopped", retries=50)
            assert stops == ["tgzr.edge.nice_client"]
            await user.should_see(marker=f"launch-{key}", retries=50)  # not running any more
            await user.should_not_see(marker=f"running-{key}")
            # It runs again (started elsewhere): the list follows within a few seconds.
            held = chain.instance_lock()
            await user.should_see(marker=f"browse-{key}", retries=50)
            user.find(marker=f"browse-{key}").click()
            await user.should_see(marker="launched", retries=50)
            assert st.navigated == ["http://127.0.0.1:7777/?launch=b"] and done == [1]
    finally:
        held.release()


async def test_stay_open_keeps_the_page_and_lists_the_new_client(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch()
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        st.stay_open = True
        await user.open("/")
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        user.find(marker="launch").click()
        await user.should_see("opened in your browser", retries=50)
        assert st.navigated == ["http://127.0.0.1:5555/?launch=tok"] and done == []  # not closing
        await user.should_not_see(marker="org-url")  # Connect closed the dialog
        await user.should_see(marker=chain_marker(home), retries=50)  # the new chain is listed


async def test_chains_added_by_another_launcher_appear(tmp_path):
    home = Home.create(tmp_path / "h")
    async with launcher_env(tmp_path, home.path, FakeLaunch()) as (user, st, done):
        await user.open("/")
        await user.should_see(marker="add-connection")
        chain = home.add_org(ORG, name="Acme").client("tgzr.edge.nice_client")
        chain.save_spec(ClientSpec("tgzr.edge.nice_client"))
        await user.should_see(marker=f"launch-{chain.org.slug}-{chain.slug}", retries=50)


async def test_a_refresh_keeps_the_open_dialog(tmp_path):
    home = Home.create(tmp_path / "h")
    async with launcher_env(tmp_path, home.path, FakeLaunch()) as (user, st, done):
        await user.open("/")
        await open_form(user)
        user.find(marker="org-url").type(ORG)
        chain = home.add_org("https://other.test", name="Other").client("tgzr.edge.nice_client")
        chain.save_spec(ClientSpec("tgzr.edge.nice_client"))  # the known clients refresh
        await user.should_see(marker=f"chain-{chain.org.slug}-{chain.slug}", retries=50)
        assert user.find(marker="org-url").elements.pop().value == ORG  # still open, still filled
