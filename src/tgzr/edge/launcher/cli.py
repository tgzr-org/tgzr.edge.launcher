"""``tgzr.edge.launcher``: start a client for an org, then exit.

Headless (``--headless``), everything comes from the flags, the environment
and the closest ``.env``; anything missing is an error. Otherwise a local
page asks for what is missing (where to create the home; the org and the
client) and lists the known (org, client) chains, each with Launch.

Exit codes: 0 launched, 1 failed, 3 the client is already running.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from tgzr.edge.home_manager import Home, HomeError, is_home

from tgzr.edge.launcher.core import (
    EXIT_FAILED,
    LaunchError,
    SpecOverrides,
    launch,
    select_chain,
    wait_for,
)
from tgzr.edge.launcher.settings import LauncherSettings, load_settings


def overrides_from(settings: LauncherSettings) -> SpecOverrides:
    return SpecOverrides(
        package=settings.client,
        version=settings.client_version,
        extras=tuple(settings.extra),
        index_urls=tuple(settings.index_url),
        find_links=tuple(settings.find_links),
        editable=tuple(settings.editable),
    )


def _say(msg: str) -> None:
    print(f"tgzr.edge.launcher: {msg}", file=sys.stderr, flush=True)


def run_headless(argv: Sequence[str] | None, settings: LauncherSettings) -> int:
    home_path = settings.home_path
    if not is_home(home_path):
        if not settings.create_home:
            raise LaunchError(f"{home_path} is not a tgzr home: create it with --create-home, or use the UI")
        Home.create(home_path)
        _say(f"created the tgzr home {home_path}")
    home = Home.open(home_path)
    if settings.wait_pid is not None:
        wait_for(settings.wait_pid, settings.wait_pid_timeout, _say)
    if not settings.org:
        raise LaunchError("--org is required with --headless")
    chain = select_chain(home, settings.org, settings.client, overrides_from(settings))
    # Settings again, from the chain folder's closest .env.
    settings = load_settings(argv, focus=chain.path)
    result = launch(home, chain, switch_to=settings.switch_to, ready_timeout=settings.ready_timeout, progress=_say)
    if result.fell_back:
        _say(f"the chosen venv failed; running venv {result.venv.number} instead")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings(argv)
    if not settings.headless:
        from tgzr.edge.launcher.ui import run_ui

        sys.exit(run_ui(argv, settings))
    try:
        code = run_headless(argv, settings)
    except LaunchError as e:
        _say(f"error: {e}" + (f" (log: {e.log_path})" if e.log_path else ""))
        sys.exit(e.exit_code)
    except (HomeError, ValueError) as e:
        _say(f"error: {e}")
        sys.exit(EXIT_FAILED)
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
