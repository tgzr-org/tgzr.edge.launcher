"""``tgzr.edge.launcher``: start a client for an org, then exit.

What is launched: ``--org`` (a URL, or the name of an org of the home) and
``--client``, else the (org, client)
launched last, else, in a home without any org, ``--default-org``.

* By default a small window (or browser tab with ``--web``) shows the
  progress, then closes (the tab goes to the client). The home is created
  when it is missing. With nothing to launch, the page below is shown.
* ``--manage``: a local page asks where to create the home, lists the known
  (org, client) chains, each with Launch, and adds connections.
* ``--headless``: no window; everything comes from the flags, the
  environment and the closest ``.env``, and anything missing is an error
  (a missing home too, unless ``--create-home``). With ``--web
  --no-browser`` the client opens no browser tab: its one-time link is
  printed, alone, on standard output (progress goes to standard error).

``--reload`` (development, implies ``--web``) is passed on to the client;
with the UI, the launcher's own page reloads too.

Exit codes: 0 launched, 1 failed, 3 the client is already running.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from tgzr.edge.home_manager import Home, HomeError, UnknownOrg, is_home

from tgzr.edge.launcher.core import (
    EXIT_FAILED,
    LaunchError,
    SpecOverrides,
    Target,
    client_env,
    default_target,
    launch,
    select_chain,
    wait_for,
)
from tgzr.edge.launcher.settings import DEFAULT_CLIENT, LauncherSettings, load_settings


def overrides_from(settings: LauncherSettings) -> SpecOverrides:
    return SpecOverrides(
        package=settings.client,
        version=settings.client_version,
        extras=tuple(settings.extra),
        index_urls=tuple(settings.index_url),
        find_links=tuple(settings.find_links),
        editable=tuple(settings.editable),
        default_index_urls=() if settings.index_url else tuple(settings.effective_index_urls),
    )


def resolve_org(settings: LauncherSettings) -> LauncherSettings:
    """``--org`` as a URL. A value that is not a URL (``http://``,
    ``https://``) is the name of an org of the home: the settings are
    returned with that org's URL. A name never adds an org. Raises
    ``UnknownOrg`` (also when there is no home yet) or ``AmbiguousOrg``."""
    org = (settings.org or "").strip()
    if not org or org.startswith(("http://", "https://")):
        return settings
    home_path = settings.home_path
    if not is_home(home_path):
        raise UnknownOrg(f"no org named {org!r}: {home_path} is not a tgzr home yet; give the org's URL")
    try:
        url = Home.open(home_path).org_named(org).url
    except UnknownOrg as e:
        raise UnknownOrg(f"{e}; give its URL to add it") from e
    return settings.model_copy(update={"org": url})


def target_from(home: Home, settings: LauncherSettings) -> Target | None:
    """``--org`` and ``--client`` as given, else :func:`default_target`."""
    if settings.org:
        return Target(settings.org, settings.client)
    return default_target(home, client=settings.client, default_org=settings.default_org,
                          default_client=DEFAULT_CLIENT)


def direct_target(settings: LauncherSettings) -> Target | None:
    """What a start without ``--headless`` or ``--manage`` launches, or
    ``None`` to show the page. A missing home is created when there is
    something to launch in it; otherwise the page asks where to create it."""
    home_path = settings.home_path
    if not is_home(home_path):
        if not (settings.org or settings.default_org.strip()):
            return None
        Home.create(home_path)
        _say(f"created the tgzr home {home_path}")
    return target_from(Home.open(home_path), settings)


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
    target = target_from(home, settings)
    if target is None:
        raise LaunchError("nothing to launch: give --org (no client was launched from this home yet)")
    chain = select_chain(home, target.org, target.client, overrides_from(settings))
    # Settings again, from the chain folder's closest .env.
    settings = load_settings(argv, focus=chain.path)
    result = launch(home, chain, switch_to=settings.switch_to, ready_timeout=settings.ready_timeout, progress=_say,
                    client_env=client_env(native=settings.use_native, hand_over_link=settings.no_browser,
                                          reload=settings.reload))
    if result.fell_back:
        _say(f"the chosen venv failed; running venv {result.venv.number} instead")
    if result.url:  # --no-browser: whoever started this launcher opens the client
        print(result.url, flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        settings = load_settings(argv)
    except ValidationError as e:
        _say(f"error: {'; '.join(err['msg'].removeprefix('Value error, ') for err in e.errors())}")
        sys.exit(EXIT_FAILED)
    try:
        settings = resolve_org(settings)
    except HomeError as e:
        _say(f"error: {e}")
        sys.exit(EXIT_FAILED)
    if not settings.headless:
        from tgzr.edge.launcher.ui import run_ui

        try:
            target = None if settings.manage else direct_target(settings)
        except (HomeError, OSError) as e:
            _say(f"error: {e}")
            sys.exit(EXIT_FAILED)
        sys.exit(run_ui(argv, settings, auto=target, overrides=overrides_from(settings)))
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
