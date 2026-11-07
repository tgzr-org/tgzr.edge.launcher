"""The launcher's page (127.0.0.1 only, behind the local access guard).

* No home yet: ask where to create it (prefilled with the resolved home dir).
* Otherwise: a form (org URL and client spec) and the known (org, client)
  chains, each with Launch. The client is started with
  ``TGZR_CLIENT_NO_BROWSER=1`` and hands its one-time link back in its ready
  signal; this tab then navigates to the client (no new tab), and the
  launcher exits.
* When the client is already running, this tab asks it for a new one-time
  link (``ClientChain.request_link``) and goes there instead of failing.
  Known clients that are running are marked so (refreshed every few
  seconds); their buttons are **Browse** and **Stop** (SIGTERM, then
  SIGKILL after a few seconds).
* **Native window** (ticked unless ``--web``) starts the client in a native
  window; unticked, in the browser.

Native is the default (``--native``): the page itself is shown in a pywebview window that
**stays open** (a dashboard over the home: it never supervises clients,
which are detached and independent of it). Links to clients (a browser
client, or Browse) open in the system browser. Several launchers may run at
once: each launch of a chain is serialized by its ``launch.lock``. With
``--web`` the page opens in the browser and closes after a launch.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from nicegui import run, ui

from tgzr.edge.home_manager import ClientChain, Home, HomeError, NoLink, NoSuchProcess, is_home

from tgzr.edge.launcher.core import (
    ClientAlreadyRunning,
    LaunchError,
    LaunchResult,
    SpecOverrides,
    client_env,
    launch,
    select_chain,
)
from tgzr.edge.launcher.guard import LaunchTokens, LocalAccessGuard
from tgzr.edge.launcher.settings import LauncherSettings, load_settings

log = logging.getLogger("tgzr.edge.launcher.ui")

TITLE = "TGZR Launcher"
RUNNING_POLL = 2.0  # seconds: how often the known clients' running state is refreshed
DEFAULT_CLIENT = "tgzr.edge.nice_client"


@dataclass
class LauncherState:
    settings: LauncherSettings
    argv: Sequence[str] | None = None
    on_done: Callable[[], None] = lambda: None
    launch_fn: Callable[..., LaunchResult] = launch
    navigate: Callable[[str], None] = field(default=lambda url: ui.navigate.to(url))  # this tab, not a new one
    link_fn: Callable[[ClientChain], str] = field(default=lambda chain: chain.request_link())
    stop_fn: Callable[[ClientChain], bool] = field(default=lambda chain: chain.stop())
    stay_open: bool = False  # native: the page stays after a launch or a browse
    done: list[LaunchResult] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)  # links to clients found already running

    @property
    def home_path(self) -> Path:
        return self.settings.home_path


_STATE: LauncherState | None = None


def state() -> LauncherState:
    if _STATE is None:  # pragma: no cover
        raise RuntimeError("tgzr.edge.launcher app not created")
    return _STATE


def create_app(st: LauncherState, *, launch_tokens: LaunchTokens, allowed_hosts: set[str], cookie_name: str) -> FastAPI:
    global _STATE
    _STATE = st
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    ui.run_with(app, root=root, title=TITLE, show_welcome_message=False, reconnect_timeout=30)
    app.add_middleware(LocalAccessGuard, launch_tokens=launch_tokens, allowed_hosts=allowed_hosts,
                       cookie_name=cookie_name, app_name=TITLE, relaunch_hint="run tgzr.edge.launcher again")
    return app


def _lines(text: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in text.replace(",", "\n").splitlines() if x.strip())


async def root() -> None:
    with ui.header().classes("items-center px-4 py-2"):
        ui.label(TITLE).classes("text-lg font-medium")
    body = ui.column().classes("w-full p-4 gap-4 items-stretch max-w-4xl mx-auto")

    def draw() -> None:
        body.clear()
        with body:
            if is_home(state().home_path):
                launch_page()
            else:
                create_home_page(draw)

    draw()


def create_home_page(redraw: Callable[[], None]) -> None:
    st = state()
    with ui.card().classes("w-full gap-3"):
        ui.label("Create your tgzr home").classes("text-xl").mark("create-home-title")
        ui.label("The home holds the orgs you use and their client environments.").classes("opacity-70")
        path = ui.input("Folder", value=str(st.home_path)).classes("w-full").mark("home-path")
        error = ui.label("").classes("text-negative").mark("create-error")

        def create() -> None:
            try:
                chosen = Path(path.value).expanduser().absolute()
                Home.create(chosen)
            except (OSError, HomeError) as e:
                error.set_text(f"Cannot create the home: {e}")
                return
            st.settings = st.settings.model_copy(update={"home_dir": chosen})
            redraw()

        ui.button("Create", on_click=create).props("color=primary").mark("create-home")


def launch_page() -> None:
    st = state()
    home = Home.open(st.home_path)
    s = st.settings
    status = ui.column().classes("w-full gap-1")

    def show_error(text: str) -> None:
        with status:
            ui.label(text).classes("text-negative whitespace-pre-wrap").mark("launch-error")

    async def open_running(chain: ClientChain, why: str) -> None:
        """Navigate this tab to a running client, through a new one-time link."""
        try:
            url = await run.io_bound(st.link_fn, chain)
        except NoLink as link_error:
            show_error(f"{why}, but it gives no link: {link_error}")
            return
        with status:
            where = "opened in your browser" if st.stay_open else "opening it..."
            ui.label(f"{chain.slug} is running for {chain.org.url}: {where}").mark("launched")
        st.opened.append(url)
        st.navigate(url)
        if not st.stay_open:
            st.on_done()

    async def do_browse(chain: ClientChain) -> None:
        status.clear()
        await open_running(chain, f"{chain.slug} is running for {chain.org.url}")

    async def confirm_stop(chain: ClientChain) -> None:
        # In ``status``, not in the known-clients list: its 2 s refresh would delete an open dialog.
        with status, ui.dialog() as dialog, ui.card().classes("gap-3"):
            ui.label(f"Stop {chain.slug} for {chain.org.name}?").classes("text-lg").mark("stop-question")
            ui.label("Its window or page closes, and work not saved in it may be lost.").classes("opacity-70")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat").mark("stop-cancel")
                ui.button("Stop", on_click=lambda: dialog.submit(True)).props("color=negative").mark("stop-confirm")
        confirmed = await dialog
        dialog.delete()
        if confirmed:
            await do_stop(chain)

    async def do_stop(chain: ClientChain) -> None:
        status.clear()
        try:
            stopped = await run.io_bound(st.stop_fn, chain)
        except NoSuchProcess as e:
            show_error(f"Could not stop it: {e}")
            return
        with status:
            ui.label(f"{chain.slug} for {chain.org.url} {'stopped' if stopped else 'was not running'}.").mark(
                "stopped")
        known_clients.refresh()

    async def do_launch(org_url: str, client: str | None, overrides: SpecOverrides) -> None:
        status.clear()
        with status:
            spinner = ui.spinner()
            lines = ui.log(max_lines=50).classes("w-full h-40").mark("progress")
        messages: list[str] = []

        def progress(msg: str) -> None:
            messages.append(msg)

        def push() -> None:
            while messages:
                lines.push(messages.pop(0))

        timer = ui.timer(0.2, push)
        try:
            # Settings for this launch come from the chain folder's closest .env.
            chain = await run.io_bound(select_chain, home, org_url, client, overrides)
            settings = load_settings(st.argv, focus=chain.path) if st.argv is not None else s
            result = await run.io_bound(st.launch_fn, home, chain, switch_to=None,
                                        ready_timeout=settings.ready_timeout, progress=progress,
                                        client_env=client_env(native=native.value, hand_over_link=True))
        except ClientAlreadyRunning as e:
            push()
            await open_running(chain, str(e))
            return
        except LaunchError as e:
            push()
            with status:
                ui.label(f"Launch failed: {e}").classes("text-negative whitespace-pre-wrap").mark("launch-error")
                if e.log_path:
                    ui.label(f"Client log: {e.log_path}").classes("opacity-70").mark("log-path")
            return
        except (HomeError, ValueError) as e:
            with status:
                ui.label(f"Launch failed: {e}").classes("text-negative").mark("launch-error")
            return
        finally:
            timer.cancel()
            spinner.delete()
        push()
        st.done.append(result)
        with status:
            if result.url:
                where = "opened in your browser" if st.stay_open else "opening it..."
                ui.label(f"{chain.slug} is running for {chain.org.url}: {where}").mark("launched")
            elif result.exited:
                ui.label(f"{chain.slug} for {chain.org.url} ran and finished.").mark("launched")
            else:  # a native window
                closes = "" if st.stay_open else " This launcher closes now."
                ui.label(f"{chain.slug} is running for {chain.org.url}.{closes}").mark("launched")
        if result.url:
            st.navigate(result.url)
        if st.stay_open:
            known_clients.refresh()
        else:
            st.on_done()

    with ui.card().classes("w-full gap-2"):
        ui.label("Launch a client").classes("text-xl")
        native = ui.checkbox("Native window (otherwise the browser)", value=s.use_native).mark("native")
        org = ui.input("Org URL", value=s.org or "", placeholder="https://org.example.com").classes("w-full").mark(
            "org-url")
        client = ui.input("Client", value=s.client or DEFAULT_CLIENT).classes("w-full").mark("client")
        version = ui.input("Client version", value=s.client_version or "", placeholder=">=0.1,<1").classes(
            "w-full").mark("client-version")
        with ui.expansion("Advanced").classes("w-full"):
            extras = ui.textarea("More requirements (one per line)", value="\n".join(s.extra)).classes(
                "w-full").mark("extras")
            default_index = s.default_index_url.strip() or "PyPI"
            indexes = ui.textarea(f"Package indexes (one per line; empty: {default_index})",
                                  value="\n".join(s.index_url)).classes(
                "w-full").mark("indexes")
            find_links = ui.textarea("Local wheel folders (one per line)", value="\n".join(s.find_links)).classes(
                "w-full").mark("find-links")
            editable = ui.textarea("Editable project folders (development; one per line)",
                                   value="\n".join(s.editable)).classes("w-full").mark("editable")
        form_error = ui.label("").classes("text-negative").mark("form-error")

        async def from_form() -> None:
            if not org.value.strip() or not client.value.strip():
                form_error.set_text("The org URL and the client are required.")
                return
            form_error.set_text("")
            await do_launch(org.value.strip(), client.value.strip(), SpecOverrides(
                package=client.value.strip(), version=version.value.strip() or None, extras=_lines(extras.value),
                index_urls=_lines(indexes.value), find_links=_lines(find_links.value),
                editable=_lines(editable.value),
                default_index_urls=tuple(s.effective_index_urls) if not _lines(indexes.value) else ()))

        ui.button("Launch", on_click=from_form).props("color=primary").mark("launch")

    shown: dict[str, list[tuple[str, bool]]] = {"snapshot": []}

    def known() -> list[ClientChain]:
        return [c for o in home.orgs() for c in o.clients() if c.spec is not None]

    def snapshot(chains: list[ClientChain]) -> list[tuple[str, bool]]:
        return [(f"{c.org.slug}/{c.slug}", c.is_running()) for c in chains]

    @ui.refreshable
    def known_clients() -> None:
        chains = known()  # other launchers may add chains
        snap = snapshot(chains)
        shown["snapshot"] = snap
        states = [running for _, running in snap]
        if not chains:
            return
        with ui.card().classes("w-full gap-2"):
            ui.label("Known clients").classes("text-xl")
            for c, running in zip(chains, states, strict=True):
                cur = c.current_number()
                key = f"{c.org.slug}-{c.slug}"
                with ui.row().classes("w-full items-center justify-between").mark(f"chain-{key}"):
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"{c.org.name}").classes("font-medium")
                            if running:
                                ui.chip("running", icon="play_circle", color="positive").props(
                                    "dense outline").mark(f"running-{key}")
                        ui.label(f"{c.slug} · venv {cur if cur is not None else '-'}").classes("opacity-70 text-sm")

                    def make(chain: ClientChain = c, is_running: bool = running) -> Callable[[], Any]:
                        if is_running:
                            return lambda: do_browse(chain)
                        return lambda: do_launch(chain.org.url, chain.slug, SpecOverrides())

                    if running:
                        with ui.row().classes("gap-1"):
                            ui.button("Browse", on_click=make()).props("flat").mark(f"browse-{key}")
                            ui.button("Stop", on_click=lambda chain=c: confirm_stop(chain)).props(
                                "flat color=negative").mark(f"stop-{key}")
                    else:
                        ui.button("Launch", on_click=make()).props("flat").mark(f"launch-{key}")

    def refresh_if_changed() -> None:
        if snapshot(known()) != shown["snapshot"]:
            known_clients.refresh()

    known_clients()
    ui.timer(RUNNING_POLL, refresh_if_changed)
    ui.label(f"Home: {home.path}").classes("opacity-60 text-sm").mark("home")
    status.move(target_index=-1)


def run_ui(argv: Sequence[str] | None, settings: LauncherSettings) -> int:
    """Serve the page until a launch succeeds (exit 0) or the user stops it
    (closes the native window, or Ctrl-C)."""
    import uvicorn

    webview: Any = None
    if settings.use_native:
        try:
            import webview  # pyright: ignore[reportMissingImports]
        except ImportError:
            print("tgzr.edge.launcher: native windows need pywebview (pip install 'pywebview[qt]'), "
                  "or use --web", file=sys.stderr)
            return 1

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", settings.port))
    port = sock.getsockname()[1]
    tokens = LaunchTokens()
    server: uvicorn.Server | None = None

    def done() -> None:  # browser mode only: native stays open
        def later() -> None:
            time.sleep(1.5)  # let the page show the result
            if server is not None:
                server.should_exit = True

        threading.Thread(target=later, daemon=True).start()

    st = LauncherState(settings=settings, argv=list(argv) if argv is not None else sys.argv[1:], on_done=done)
    if settings.use_native:
        st.stay_open = True
        st.navigate = webbrowser.open  # the window stays a launcher: clients open in the system browser
    app = create_app(st, launch_tokens=tokens, allowed_hosts={f"127.0.0.1:{port}"},
                     cookie_name=f"tgzr.edge.launcher_{port}")
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    url = f"http://127.0.0.1:{port}/?launch={tokens.issue()}"

    def opened() -> None:
        while not server.started:
            if server.should_exit:
                return
            time.sleep(0.05)
        if settings.no_browser:
            print(f"{TITLE}: open this one-time link (valid 5 minutes):\n{url}", flush=True)
        else:
            webbrowser.open(url)

    if webview is not None:
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        while not server.started:
            if not thread.is_alive():
                print("tgzr.edge.launcher: the local server failed to start", file=sys.stderr)
                return 1
            time.sleep(0.05)
        webview.create_window(TITLE, url, width=900, height=820)
        webview.start()  # until the user closes the window
        server.should_exit = True
        thread.join(timeout=5)
        return 0
    threading.Thread(target=opened, daemon=True).start()
    server.run(sockets=[sock])
    return 0 if st.done or st.opened else 1
