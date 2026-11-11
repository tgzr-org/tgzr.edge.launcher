"""``LauncherSettings``: flag > env (``TGZR_LAUNCHER_*``) > closest ``.env`` > default.

The home comes from ``--home-dir`` or ``TGZR_HOME``, never from a ``.env``
file (it decides which ``.env`` is the closest one).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, CliToggleFlag, SettingsConfigDict

from tgzr.edge.home_manager import ENV_HOME, closest_env_file, home_dir_from, is_home

# Where new clients install from when no --index-url is given (empty: PyPI).
DEFAULT_INDEX_URL = "https://pi.alpha.tgzr.net/tgzr/beta"


class LauncherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TGZR_LAUNCHER_",
        extra="ignore",
        cli_prog_name="tgzr.edge.launcher",
        cli_kebab_case=True,
        cli_hide_none_type=True,
        cli_avoid_json=True,
    )

    home_dir: Path | None = Field(None, description="the tgzr home (env TGZR_HOME; default ~/tgzr_home)")
    create_home: CliToggleFlag[bool] = Field(False, description="headless: create the home if it does not exist")
    org: str | None = Field(None, description="the org URL")
    client: str | None = Field(None, description="the client distribution, e.g. tgzr.edge.nice_client")
    client_version: str | None = Field(None, description="a version specifier for the client, e.g. '>=0.1,<1'")
    extra: list[str] = Field([], description="more requirements for the client venv (repeatable)")
    index_url: list[str] = Field([], description="package index for the client (repeatable; default: --default-index-url)")
    default_index_url: str = Field(
        DEFAULT_INDEX_URL,
        description=f"the index for a new client when no --index-url is given (default {DEFAULT_INDEX_URL}; empty: PyPI)")
    find_links: list[str] = Field([], description="a folder of local wheels (repeatable)")
    editable: list[str] = Field([], description="install from this local project folder, editable (repeatable)")
    headless: CliToggleFlag[bool] = Field(False, description="no UI: launch from the flags, or fail")
    native: CliToggleFlag[bool] = Field(
        False, description="native windows for this launcher and the clients it starts (the default)")
    web: CliToggleFlag[bool] = Field(
        False, description="the browser instead: this launcher's page, and the clients it starts")
    reload: CliToggleFlag[bool] = Field(
        False, description="development: restart this launcher's page and the clients it starts when a Python file "
                           "of a tgzr package changes (implies --web)")
    switch_to: int | None = Field(None, description="make venv N current before starting (used by client restarts)")
    wait_pid: int | None = Field(None, description="first wait until this process has exited")
    wait_pid_timeout: float = Field(60.0, description="seconds to wait for --wait-pid")
    ready_timeout: float = Field(60.0, description="seconds to wait for the client's ready signal")
    port: int = Field(0, description="the launcher UI's local port (default: a free one)")
    no_browser: CliToggleFlag[bool] = Field(False, description="UI: print a one-time link instead of opening the browser")

    @classmethod
    def settings_customise_sources(
        cls, settings_cls: Any, init_settings: Any, env_settings: Any, dotenv_settings: Any, file_secret_settings: Any
    ) -> tuple[Any, ...]:
        def env() -> dict[str, Any]:
            values = {k: v for k, v in env_settings().items() if k != "home_dir"}
            if os.environ.get(ENV_HOME):
                values["home_dir"] = os.environ[ENV_HOME]
            return values

        def dotenv() -> dict[str, Any]:
            return {k: v for k, v in dotenv_settings().items() if k != "home_dir"}

        return init_settings, env, dotenv, file_secret_settings

    @model_validator(mode="after")
    def _one_mode(self) -> LauncherSettings:
        if self.native and self.web:
            raise ValueError("give --native or --web, not both")
        if self.native and self.reload:
            raise ValueError("--reload implies --web: do not give --native with it")
        return self

    @property
    def use_native(self) -> bool:
        """Native windows unless ``--web`` or ``--reload`` (or ``TGZR_LAUNCHER_WEB``, ``TGZR_LAUNCHER_RELOAD``)."""
        return not (self.web or self.reload)

    @property
    def effective_index_urls(self) -> list[str]:
        """``--index-url`` (or ``TGZR_LAUNCHER_INDEX_URL``) if given, else the
        default index, else nothing (uv's default: PyPI)."""
        if self.index_url:
            return list(self.index_url)
        return [self.default_index_url] if self.default_index_url.strip() else []

    @property
    def home_path(self) -> Path:
        return home_dir_from(self.home_dir)


def load_settings(argv: Sequence[str] | None = None, focus: Path | None = None) -> LauncherSettings:
    """Parse ``argv`` and the environment, then load again with the closest
    ``.env`` from ``focus`` (default: the home, when it exists)."""
    args = list(argv) if argv is not None else sys.argv[1:]
    boot = LauncherSettings(_cli_parse_args=args)  # type: ignore[call-arg]
    home = boot.home_path
    if focus is None:
        if not is_home(home):
            return boot
        focus = home
    env_file = closest_env_file(focus, home)
    return LauncherSettings(_cli_parse_args=args, _env_file=env_file)  # type: ignore[call-arg]
