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
  `--index-url`, `--find-links`, `--editable` (repeatable), `--headless`,
  `--switch-to N`, `--wait-pid PID`, `--wait-pid-timeout`, `--ready-timeout`
  (60 s), `--port`, `--no-browser`.
- The first launch saves the client spec in `client.json`; later launches
  reuse it, and flags given again override it.
- **Exit codes**: 0 launched (or the client finished with 0), 1 failed (the
  message names the client log), 3 the client is already running.
- The client gets `TGZR_HOME`, `TGZR_ORG`, `TGZR_CLIENT` and `TGZR_LAUNCHER`,
  so it can restart itself through the launcher after an update.

uv must be on `PATH`.

## Tests

```bash
cd packages/tgzr.edge.launcher
uv run --project ../../workspace_dev pytest          # -m "not slow" skips the uv end-to-end test
```
