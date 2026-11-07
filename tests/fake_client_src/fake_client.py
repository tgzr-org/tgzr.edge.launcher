"""A stdlib-only stand-in for a client, run as ``<venv>/bin/python -m fake_client``.

What it does is read from ``<venv>/behavior``: ``ready`` (report ready, then
wait to be killed), ``crash`` (exit 2), ``hang`` (never ready), ``exit0``
(exit 0 at once). Like a real client it holds ``instance.lock`` (flock) and
exits 3 when it cannot take it.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

venv = Path(sys.executable).parent.parent
chain = venv.parent
behavior = (venv / "behavior").read_text().strip()
lock = open(chain / "instance.lock", "a")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("already running", file=sys.stderr)
    sys.exit(3)
print(f"fake client venv={venv.name} behavior={behavior} org={os.environ['TGZR_ORG']} "
      f"launcher={os.environ.get('TGZR_LAUNCHER')}", flush=True)
if behavior == "crash":
    sys.exit(2)
if behavior == "exit0":
    sys.exit(0)
if behavior == "ready":
    tmp = chain / ".instance.json.tmp"
    tmp.write_text(json.dumps({"pid": os.getpid(), "n": int(venv.name), "client_version": "0.0.1",
                               "started_at": "now", "state": "ready",
                               "url": "http://127.0.0.1:1/?launch=fake" if os.environ.get("TGZR_CLIENT_NO_BROWSER") else None}))
    os.replace(tmp, chain / "instance.json")
time.sleep(600)
