"""The launcher's page in process (NiceGUI user simulation); launches are faked."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from nicegui.testing.general import nicegui_reset_globals
from nicegui.testing.user import User

from tgzr.edge.home_manager import ClientSpec, Home, Venv

from tgzr.edge.launcher.core import LaunchError, LaunchResult
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


@asynccontextmanager
async def launcher_env(tmp_path, home_dir, launch_fn):
    os.environ["NICEGUI_USER_SIMULATION"] = "true"
    done: list[int] = []
    st = LauncherState(settings=LauncherSettings(home_dir=home_dir), argv=None, on_done=lambda: done.append(1),
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
        await user.should_see(marker="launch", retries=20)
        assert (tmp_path / "chosen" / "tgzr_home.json").is_file()
        assert st.home_path == tmp_path / "chosen"


async def test_launch_from_the_form(tmp_path):
    home = Home.create(tmp_path / "h")
    fake = FakeLaunch()
    async with launcher_env(tmp_path, home.path, fake) as (user, st, done):
        await user.open("/")
        user.find(marker="org-url").type(ORG)
        user.find(marker="client-version").type(">=0.1,<1")
        user.find(marker="launch").click()
        await user.should_see(marker="launched", retries=50)
        assert fake.calls == [(ORG, "tgzr.edge.nice_client")] and done == [1]
        assert fake.client_envs == [{"TGZR_CLIENT_NO_BROWSER": "1"}]  # the client hands its link back
        assert st.navigated == ["http://127.0.0.1:5555/?launch=tok"]  # this tab goes there
        assert home.org(ORG).client("tgzr.edge.nice_client").spec.version == ">=0.1,<1"


async def test_form_needs_an_org(tmp_path):
    home = Home.create(tmp_path / "h")
    async with launcher_env(tmp_path, home.path, FakeLaunch()) as (user, st, done):
        await user.open("/")
        user.find(marker="launch").click()
        await user.should_see("The org URL and the client are required.")


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
