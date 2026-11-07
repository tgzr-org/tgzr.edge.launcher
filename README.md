# tgzr.edge.launcher

Starts a TGZR client for an org, then exits (org spec D51). It creates the
tgzr home, builds the client's first venv with uv, starts the client
detached, waits for its ready signal, and falls back to the previous venv
when a new one fails.

```bash
tgzr.edge.launcher                          # a local page: create the home, launch a client
tgzr.edge.launcher --headless --create-home --home-dir ~/tgzr_home \
    --org https://org.example.com --client tgzr.edge.nice_client --client-version ">=0.1,<1"
```

Development, from `workspace_dev` (the client and its unpublished siblings,
editable):

```bash
uv run tgzr.edge.launcher --home-dir /tmp/th --org http://127.0.0.1:8000 --client tgzr.edge.nice_client \
    --editable ../packages/tgzr.edge.nice_client --editable ../packages/tgzr.edge.home_manager \
    --editable ../packages/tgzr.libs.package_management --editable ../packages/tgzr.orgs.connect \
    --editable ../packages/tgzr.orgs.client
```

- **Settings** (`LauncherSettings`, flag > `TGZR_LAUNCHER_*` > closest `.env`
  > default): `--home-dir` (or `TGZR_HOME`, never from `.env`),
  `--create-home`, `--org`, `--client`, `--client-version`, `--extra`,
  `--index-url`, `--find-links`, `--editable` (repeatable), `--default-index-url`,
  `--headless`,
  `--native` (the default: this page in a native window that stays open, and
  the clients it starts in native windows too; links to browser clients open
  in the system browser) or `--web` (this page in the browser, and web
  clients; giving both is an error),
  `--switch-to N`, `--wait-pid PID`, `--wait-pid-timeout`, `--ready-timeout`
  (60 s), `--port`, `--no-browser`.
- **Package index of a new client:** `--index-url` (or `TGZR_LAUNCHER_INDEX_URL`,
  a JSON list), else `--default-index-url` (or `TGZR_LAUNCHER_DEFAULT_INDEX_URL`;
  default `https://pi.alpha.tgzr.net/tgzr/beta`), else PyPI when that is empty.
  A client launched before keeps the index saved in its `client.json`, unless
  `--index-url` is given.
- **The page**: a launch form (with a *Native window* checkbox) and the known
  clients. A running client is marked *running* and has **Browse** (this tab
  opens it through a new one-time link) and **Stop** (after a confirmation; SIGTERM, then SIGKILL
  after 10 s). The list follows changes made elsewhere (every 2 s). In the
  browser, launching navigates the tab to the client and the launcher exits;
  in the native window, the launcher stays open. A client found already
  running is opened instead.
- **Several launchers may run at once.** Each launch of an (org, client) holds
  its `launch.lock` from the "already running?" check until the client is
  ready or failed; another launcher waits (up to 15 min: it may be building
  the first venv), then finds the client running.
- The first launch saves the client spec in `client.json`; later launches
  reuse it, and flags given again override it.
- **Exit codes**: 0 launched (or the client finished with 0), 1 failed (the
  message names the client log), 3 the client is already running.
- The client gets `TGZR_HOME`, `TGZR_ORG`, `TGZR_CLIENT` and `TGZR_LAUNCHER`,
  so it can restart itself through the launcher after an update.

uv comes with `tgzr.libs.package_management` (the `uv` package); set
`TGZR_UV` to use another one.

## Executable

`packaging/pyapp/build.py` builds a one-file executable with
[PyApp](https://ofek.dev/pyapp/): a small stub that, on its first run,
downloads Python, installs `tgzr.edge.launcher==<version>` from the package
index baked into it (with uv), then runs `python -m tgzr.edge.launcher`. Later
runs start directly. Clients restart the launcher through the executable
(`PYAPP` holds its path).

```bash
python packaging/pyapp/build.py --version 0.101      # needs cargo; writes dist-exe/
python packaging/pyapp/build.py --help               # --index-url, --find-links, --python, --pyapp-version
```

- The executable installs exactly `--version` from `--index-url` (default
  `https://pi.alpha.tgzr.net/tgzr/beta`), so **upload that version to
  the index first**.
- On Windows it is built as a GUI program (no console window). PyApp then
  starts `pythonw.exe` without waiting for it, so in a terminal `--help` and
  other output are not shown and the exit code is lost (`self ...` commands
  still work).
- Its install lives in PyApp's per-user data folder; `PYAPP_INSTALL_DIR_TGZR-EDGE-LAUNCHER`
  (set with `env`: the name has dashes) moves it. `<exe> self remove` deletes it, `<exe> self restore` reinstalls.
- **CI:** pushing a `v*` tag runs `.github/workflows/executables.yml`, which
  builds Linux and Windows x86_64 executables and attaches them to the GitHub
  release of that tag. The repository is private, so download them from the
  release and host them where users can get them.

## Tests

```bash
cd packages/tgzr.edge.launcher
uv run --project ../../workspace_dev pytest          # -m "not slow" skips the uv end-to-end test
```
