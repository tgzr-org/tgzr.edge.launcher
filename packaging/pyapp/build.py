"""Build the one-file launcher executable with PyApp (https://ofek.dev/pyapp/).

The executable is a small Rust stub: on its first run it downloads Python,
installs ``tgzr.edge.launcher==<version>`` from the index baked in here (with
uv), then runs ``python -m tgzr.edge.launcher``.

Needs cargo (https://rustup.rs). Standard library only, so CI can run it
with any Python >= 3.11:

    python packaging/pyapp/build.py --version 0.101
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the launcher repository
DISTRIBUTION = "tgzr.edge.launcher"
DEFAULT_INDEX = "https://pi.alpha.tgzr.net/tgzr/beta"
DEFAULT_PYAPP = "0.31.0"
DEFAULT_PYTHON = "3.12"


class BuildError(Exception):
    pass


def tag_version() -> str:
    """The version of the tag on HEAD (``v0.101`` -> ``0.101``)."""
    res = subprocess.run(["git", "describe", "--tags", "--exact-match"], cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError("HEAD has no tag: give --version (it must be a version uploaded to the index)")
    return res.stdout.strip().removeprefix("v")


def find_cargo(explicit: str | None) -> str:
    candidates = [explicit] if explicit else [shutil.which("cargo"), str(Path.home() / ".cargo" / "bin" / "cargo")]
    for c in candidates:
        if c and Path(c).is_file():
            return c
        if c and shutil.which(c):
            return shutil.which(c)  # type: ignore[return-value]
    raise BuildError("cargo not found: install Rust (https://rustup.rs) or give --cargo")


def pyapp_source(version: str, build_dir: Path) -> Path:
    """Download and unpack the PyApp source once per version."""
    src = build_dir / f"pyapp-{version}"
    if (src / "Cargo.toml").is_file():
        return src
    archive = build_dir / f"pyapp-{version}-source.tar.gz"
    if not archive.is_file():
        url = f"https://github.com/ofek/pyapp/releases/download/v{version}/source.tar.gz"
        print(f"downloading {url}", flush=True)
        with urllib.request.urlopen(url) as r, open(archive, "wb") as f:  # noqa: S310 (fixed https URL)
            shutil.copyfileobj(r, f)
    tmp = build_dir / f"pyapp-{version}-unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(archive) as tar:
        tar.extractall(tmp, filter="data")
    roots = [p for p in tmp.iterdir() if (p / "Cargo.toml").is_file()]
    if len(roots) != 1:
        raise BuildError(f"unexpected PyApp source layout in {archive}")
    roots[0].rename(src)
    shutil.rmtree(tmp)
    return src


def requirements(version: str, index_url: str, find_links: list[str]) -> str:
    lines = [f"--index-url {index_url}"]
    lines += [f"--find-links {Path(fl).resolve().as_uri() if '://' not in fl else fl}" for fl in find_links]
    lines.append(f"{DISTRIBUTION}=={version}")
    return "\n".join(lines) + "\n"


def target_name(version: str) -> str:
    system = {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    suffix = ".exe" if sys.platform == "win32" else ""
    return f"tgzr-launcher-{version}-{system}-{machine}{suffix}"


def build(args: argparse.Namespace) -> Path:
    version = args.version or tag_version()
    cargo = find_cargo(args.cargo)
    build_dir = (ROOT / "build-exe").resolve()
    build_dir.mkdir(exist_ok=True)
    src = pyapp_source(args.pyapp_version, build_dir)

    reqs = requirements(version, args.index_url, args.find_links)
    # A content-named file: changing it changes PYAPP_PROJECT_DEPENDENCY_FILE, so cargo rebuilds.
    reqs_file = build_dir / f"requirements-{hashlib.sha256(reqs.encode()).hexdigest()[:12]}.txt"
    reqs_file.write_text(reqs)

    env = dict(os.environ)
    env.update({
        "PYAPP_PROJECT_NAME": DISTRIBUTION,
        "PYAPP_PROJECT_VERSION": version,
        "PYAPP_PROJECT_DEPENDENCY_FILE": str(reqs_file),
        "PYAPP_EXEC_MODULE": DISTRIBUTION,
        "PYAPP_PYTHON_VERSION": args.python,
        "PYAPP_UV_ENABLED": "1",
        "PYAPP_PASS_LOCATION": "1",  # PYAPP=<executable path>: clients restart the launcher through it
    })
    if sys.platform == "win32":
        env["PYAPP_IS_GUI"] = "1"  # no console window
    print(f"building {DISTRIBUTION}=={version} (PyApp {args.pyapp_version}, Python {args.python})\n"
          f"requirements:\n{reqs}", flush=True)
    res = subprocess.run([cargo, "build", "--release", "--locked"], cwd=src, env=env)
    if res.returncode != 0:
        raise BuildError(f"cargo build failed ({res.returncode})")

    binary = src / "target" / "release" / ("pyapp.exe" if sys.platform == "win32" else "pyapp")
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / target_name(version)
    shutil.copy2(binary, out)
    out.chmod(0o755)
    print(f"built {out} ({out.stat().st_size / 1e6:.1f} MB)", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", help="the launcher version to install (default: the tag on HEAD)")
    p.add_argument("--index-url", default=DEFAULT_INDEX, help=f"the package index (default {DEFAULT_INDEX})")
    p.add_argument("--find-links", action="append", default=[],
                   help="also install from this wheel folder or URL (repeatable; for trial builds)")
    p.add_argument("--python", default=DEFAULT_PYTHON, help=f"the Python the executable installs (default {DEFAULT_PYTHON})")
    p.add_argument("--pyapp-version", default=DEFAULT_PYAPP, help=f"default {DEFAULT_PYAPP}")
    p.add_argument("--cargo", help="the cargo executable (default: PATH, then ~/.cargo/bin/cargo)")
    p.add_argument("--out", default=str(ROOT / "dist-exe"), help="output folder (default dist-exe/)")
    args = p.parse_args(argv)
    try:
        build(args)
    except BuildError as e:
        print(f"build.py: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
