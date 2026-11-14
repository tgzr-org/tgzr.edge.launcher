# tgzr.edge.launcher

Starts a TGZR client for an org, then exits (org spec D51, D59). It creates
the tgzr home, builds the client's first venv with uv, starts the client
detached, waits for its ready signal, and falls back to the previous venv
when a new one fails.

```bash
tgzr.edge.launcher                          # launch the client used last, showing only the progress
tgzr.edge.launcher --org https://org.example.com --client tgzr.edge.nice_client   # launch this one
tgzr.edge.launcher --manage                 # the page: create the home, the known connections, add one
tgzr.edge.launcher --headless --create-home --home-dir ~/tgzr_home \
    --org https://org.example.com --client tgzr.edge.nice_client --client-version ">=0.1,<1"
```

**What is launched:** `--org` (and `--client`), else the (org, client)
launched last (`last_launch` in `tgzr_home.json`), else, in a home without
any org, `--default-org`. The built-in default org is empty until the
community org is hosted; set `TGZR_LAUNCHER_DEFAULT_ORG` (or `--default-org`)
to choose one, or to an empty value to have none.

`--org` takes a URL, or the name of an org already in the home (`--org Acme`;
case is ignored). A name that matches no org, or several, is an error: only
a URL adds an org.

**Three ways to run:**

- **Direct start** (no `--manage`, no `--headless`): a small window, or a
  browser tab with `--web`, shows the progress of the launch. On success the
  window closes (the tab goes to the client). The home is created when it is
  missing. If the launch fails, the window stays with the error and a
  **Manage connections** button leading to the page. With nothing to launch
  (no `--org`, no last launch, no default org), the page is shown.
- **`--manage`**: the page (below).
- **`--headless`**: no window. A missing home is an error unless
  `--create-home`. With `--web --no-browser`, the client opens no browser
  tab: its one-time link is printed alone on standard output (progress goes
  to standard error). This is how the Orgs Connections app
  ([`tgzr.edge.nice_apps.orgs_connections`](../tgzr.edge.nice_apps.orgs_connections))
  opens another org from inside a client.

**Development.** Use a home of its own (`--home-dir ~/tgzr_home_dev`), not
your daily one.

- Changing the tgzr packages themselves: from `workspace_dev`, with the
  client and its unpublished siblings editable (see its README):

  ```bash
  uv run tgzr.edge.launcher --home-dir ~/tgzr_home_dev --org http://127.0.0.1:8080 --client tgzr.edge.nice_client \
      --editable ../packages/tgzr.edge.nice_client --editable ../packages/tgzr.edge.home_manager \
      --editable ../packages/tgzr.libs.package_management --editable ../packages/tgzr.libs.nice \
      --editable ../packages/tgzr.orgs.connect --editable ../packages/tgzr.orgs.client
  ```

- Writing your own app plugin: the released client plus your app, editable
  (see the `tgzr-nice-app` skill):

  ```bash
  tgzr.edge.launcher --home-dir ~/tgzr_home_dev --org https://org.example.com --client tgzr.edge.nice_client \
      --extra tgzr.edge.nice_client --editable ../acme-tickets-app --reload
  ```

`--editable` behaves in ways worth knowing:

- With any `--editable`, the client's own requirement is left out of the
  venv: the client is expected to be one of the editables. When it is not
  (the second case above), name it with `--extra`, or the client fails to
  start ("No module named ...").
- It is used when a venv is **built**. On a connection that already has a
  venv it is saved in `client.json` but not applied: add the folder on the
  client's Packages page (`/apps/manage/packages`, installed editable), or
  remove the connection and launch again.
- Giving it again replaces the whole saved list, and no flag removes one.

- **Settings** (`LauncherSettings`, flag > `TGZR_LAUNCHER_*` > closest `.env`
  > default): `--home-dir` (or `TGZR_HOME`, never from `.env`),
  `--create-home`, `--org`, `--default-org`, `--manage`, `--client`, `--client-version`, `--extra`,
  `--index-url`, `--find-links`, `--editable` (repeatable), `--default-index-url`,
  `--headless`,
  `--native` (the default: this page in a native window that stays open, and
  the clients it starts in native windows too; links to browser clients open
  in the system browser) or `--web` (this page in the browser, and web
  clients; giving both is an error),
  `--switch-to N`, `--wait-pid PID`, `--wait-pid-timeout`, `--ready-timeout`
  (60 s), `--port`, `--no-browser`, `--reload` (development, implies `--web`:
  this page restarts when a `.py` file of an editable install or of the `tgzr`
  namespace changes, and the clients it starts get `--reload` too).
- **Package index of a new client:** `--index-url` (or `TGZR_LAUNCHER_INDEX_URL`,
  a JSON list), else `--default-index-url` (or `TGZR_LAUNCHER_DEFAULT_INDEX_URL`;
  default `https://pi.alpha.tgzr.net/tgzr/beta`), else PyPI when that is empty.
  A client launched before keeps the index saved in its `client.json`, unless
  `--index-url` is given.
- A new org is asked for its name (its public `/info`); when it does not
  answer, its URL is shown instead.
- **The page** (`--manage`): a launch form (with a *Native window* checkbox) and the known
  clients. The one a start without `--org` launches (the one launched last) is
  marked *default*. A running client is marked *running* and has **Browse** (this tab
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
