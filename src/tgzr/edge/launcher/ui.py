"""The launcher's page (127.0.0.1 only, behind the local access guard).

* No home yet: ask where to create it (prefilled with the resolved home dir).
* Otherwise: a form (org URL and client spec) and the known (org, client)
  chains, each with Launch. The client is started with
  ``TGZR_CLIENT_NO_BROWSER=1`` and hands its one-time link back in its ready
  signal; this tab then navigates to the client (no new tab), and the
  launcher exits.
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

from tgzr.edge.home_manager import ClientChain, Home, HomeError, is_home

from tgzr.edge.launcher.core import LaunchError, LaunchResult, SpecOverrides, launch, select_chain
from tgzr.edge.launcher.guard import LaunchTokens, LocalAccessGuard
from tgzr.edge.launcher.settings import LauncherSettings, load_settings

log = logging.getLogger("tgzr.edge.launcher.ui")

TITLE = "TGZR Launcher"
# Ask the client for its link instead of a new browser tab.
CLIENT_ENV = {"TGZR_CLIENT_NO_BROWSER": "1"}
DEFAULT_CLIENT = "tgzr.edge.nice_client"


@dataclass
class LauncherState:
    settings: LauncherSettings
    argv: Sequence[str] | None = None
    on_done: Callable[[], None] = lambda: None
    launch_fn: Callable[..., LaunchResult] = launch
    navigate: Callable[[str], None] = field(default=lambda url: ui.navigate.to(url))  # this tab, not a new one
    done: list[LaunchResult] = field(default_factory=list)

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
                                        client_env=CLIENT_ENV)
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
                ui.label(f"{chain.slug} is running for {chain.org.url}: opening it...").mark("launched")
            else:  # a native window, or a client that already finished
                ui.label(f"{chain.slug} is running for {chain.org.url}. This launcher closes now.").mark("launched")
        if result.url:
            st.navigate(result.url)
        st.on_done()

    with ui.card().classes("w-full gap-2"):
        ui.label("Launch a client").classes("text-xl")
        org = ui.input("Org URL", value=s.org or "", placeholder="https://org.example.com").classes("w-full").mark(
            "org-url")
        client = ui.input("Client", value=s.client or DEFAULT_CLIENT).classes("w-full").mark("client")
        version = ui.input("Client version", value=s.client_version or "", placeholder=">=0.1,<1").classes(
            "w-full").mark("client-version")
        with ui.expansion("Advanced").classes("w-full"):
            extras = ui.textarea("More requirements (one per line)", value="\n".join(s.extra)).classes(
                "w-full").mark("extras")
            indexes = ui.textarea("Package indexes (one per line; default PyPI)", value="\n".join(s.index_url)).classes(
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
                editable=_lines(editable.value)))

        ui.button("Launch", on_click=from_form).props("color=primary").mark("launch")

    chains: list[ClientChain] = [c for o in home.orgs() for c in o.clients() if c.spec is not None]
    if chains:
        with ui.card().classes("w-full gap-2"):
            ui.label("Known clients").classes("text-xl")
            for c in chains:
                cur = c.current_number()
                with ui.row().classes("w-full items-center justify-between").mark(f"chain-{c.org.slug}-{c.slug}"):
                    with ui.column().classes("gap-0"):
                        ui.label(f"{c.org.name}").classes("font-medium")
                        ui.label(f"{c.slug} · venv {cur if cur is not None else '-'}"
                                 + (" · running" if c.is_running() else "")).classes("opacity-70 text-sm")

                    def make(chain: ClientChain = c) -> Callable[[], Any]:
                        return lambda: do_launch(chain.org.url, chain.slug, SpecOverrides())

                    ui.button("Launch", on_click=make()).props("flat").mark(f"launch-{c.org.slug}-{c.slug}")
    ui.label(f"Home: {home.path}").classes("opacity-60 text-sm").mark("home")
    status.move(target_index=-1)


def run_ui(argv: Sequence[str] | None, settings: LauncherSettings) -> int:
    """Serve the page until a launch succeeds (exit 0) or the user stops it."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", settings.port))
    port = sock.getsockname()[1]
    tokens = LaunchTokens()
    server: uvicorn.Server | None = None

    def done() -> None:
        def later() -> None:
            time.sleep(1.5)  # let the page show the result
            if server is not None:
                server.should_exit = True

        threading.Thread(target=later, daemon=True).start()

    st = LauncherState(settings=settings, argv=list(argv) if argv is not None else sys.argv[1:], on_done=done)
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

    threading.Thread(target=opened, daemon=True).start()
    server.run(sockets=[sock])
    return 0 if st.done else 1
