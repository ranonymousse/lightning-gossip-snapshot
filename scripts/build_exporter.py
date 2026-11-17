"""Build and test against the pinned upstream module, without running a node."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def run(*args, cwd=None, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def check_build_tools():
    missing = [name for name in ("git", "go") if shutil.which(name) is None]
    if missing:
        raise SystemExit(
            f"Missing build prerequisites on PATH: {', '.join(missing)}.\n"
            "uv manages Python dependencies; the exporter also needs Git and Go >= 1.21.\n"
            "On Ubuntu/Debian: sudo apt install git golang-go build-essential\n"
            "Then check `go version` and rerun ./run.sh build.\n"
            "If Go is already installed, add its bin directory to PATH.\n"
            "See README.md: Build the offline exporter."
        )
    # Inspect the bootstrap without downloading a toolchain or touching the checkout.
    try:
        version = subprocess.check_output(
            ["go", "version"],
            env={**os.environ, "GOTOOLCHAIN": "local"},
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.output.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise SystemExit(
            f"Cannot run Go: {detail}\nCheck `GOTOOLCHAIN=local go version`."
        ) from None
    match = re.search(r"\bgo(\d+)\.(\d+)", version)
    if match is None or tuple(map(int, match.groups())) < (1, 21):
        raise SystemExit(
            f"Go bootstrap >= 1.21 is required for pinned toolchain selection; found: {version}.\n"
            "Install a current Go release from https://go.dev/doc/install and put it on PATH.\n"
            "Then rerun ./run.sh build; the existing LND checkout can be reused."
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, help="existing disposable pinned LND checkout")
    parser.add_argument(
        "--with-node", action="store_true", help="also compile lnd/lncli, never start"
    )
    args = parser.parse_args()
    check_build_tools()
    root = Path(__file__).resolve().parents[1]
    versions = json.loads((root / "versions.json").read_text())
    source = (args.source or root / ".cache" / "lnd").resolve()
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        run(
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            versions["lnd_repository"],
            str(source),
        )
        run("git", "checkout", "--detach", versions["lnd_commit"], cwd=source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if commit != versions["lnd_commit"]:
        raise SystemExit("Wrong LND revision; use a disposable checkout at the pinned commit.")
    run("git", "diff", "--exit-code", "HEAD", "--", ".", cwd=source)
    destination = source / "cmd" / "gossip-export"
    destination.mkdir(exist_ok=True)
    for path in (root / "cmd" / "gossip-export").glob("*.go"):
        shutil.copy2(path, destination / path.name)
    env = os.environ.copy()
    env.setdefault("GOTOOLCHAIN", versions["go_release_toolchain"])
    env.setdefault("GOCACHE", str(root / ".cache" / "go-build"))
    env.setdefault("GOMODCACHE", str(root / ".cache" / "go-mod"))
    binary_dir = root / "bin"
    binary_dir.mkdir(exist_ok=True)
    bridge = root / ".cache" / "go-fixture"
    if bridge.exists():
        # Only our generated fixture directory; never a user's capture path.
        shutil.rmtree(bridge)
    env["GOSSIP_TEST_EXPORT"] = str(bridge)
    run("go", "test", "-mod=readonly", "-count=1", "./cmd/gossip-export", cwd=source, env=env)
    # Verify the actual Go/DB-produced bytes in the independent Python reader.
    from gossip_snapshot.schema import Record
    from gossip_snapshot.wire import verify_record

    rows = (bridge / "records.jsonl").read_text().splitlines()
    if len(rows) != 1 or verify_record(Record.model_validate_json(rows[0])) is not None:
        raise SystemExit("Go/Python fixture validation failed")
    names = ["gossip-export"] + (["lnd", "lncli"] if args.with_node else [])
    for name in names:
        run(
            "go",
            "build",
            "-mod=readonly",
            "-trimpath",
            "-o",
            str(binary_dir / name),
            f"./cmd/{name}",
            cwd=source,
            env=env,
        )
    hashes = {}
    for name in names:
        with (binary_dir / name).open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    metadata = {
        "lnd_commit": commit,
        "go_version": subprocess.check_output(["go", "version"], env=env, text=True).strip(),
        "binary_sha256": hashes,
        "exporter_source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "cmd" / "gossip-export").glob("*.go"))
        },
    }
    (binary_dir / "build-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print("Built and tested offline exporter; Go/Python fixture signatures agree.")


if __name__ == "__main__":
    main()
