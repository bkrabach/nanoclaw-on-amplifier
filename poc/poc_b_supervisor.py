#!/usr/bin/env python3
"""POC-B supervisor: spawn amplifierd, wait healthy, run the Bun TS smoke test, tear down."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = 18410
HOME = Path.home()
AMP_BIN = HOME / ".nanoclaw-amp" / "bin" / "amplifierd"
BUN_BIN = HOME / ".bun" / "bin" / "bun"
REPO = Path("/home/bkrabach/dev/aaa-claw/nanoclaw-on-amplifier")
BUNDLE_PATH = REPO / "poc" / "bundle.local.md"
BUNDLE_NAME = "nanoclaw-amp-local"
LOG_PATH = REPO / "poc" / ".amplifierd.log"
SMOKE_TS = REPO / "nanoclaw-provider" / "_smoke.ts"

def banner(t: str) -> None:
    print(f"\n===== {t} =====", flush=True)


def start_daemon() -> subprocess.Popen[bytes]:
    banner(f"Spawn amplifierd :{PORT}")
    LOG_PATH.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [
        str(AMP_BIN), "serve",
        "--port", str(PORT),
        "--bundle", f"{BUNDLE_NAME}=file://{BUNDLE_PATH}",
        "--default-bundle", BUNDLE_NAME,
        "--log-level", "info",
    ]
    proc = subprocess.Popen(
        cmd, stdout=LOG_PATH.open("wb"), stderr=subprocess.STDOUT,
        env=env, start_new_session=True,
    )
    print(f"  PID {proc.pid}")
    return proc


def wait_healthy(timeout_s: int = 180) -> None:
    banner("Wait /health")
    deadline = time.monotonic() + timeout_s
    last_size = 0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=2.0)
            if r.status_code == 200:
                print(f"  ✓ healthy: {r.text[:80]}", flush=True)
                return
        except Exception:
            pass
        if LOG_PATH.exists():
            sz = LOG_PATH.stat().st_size
            if sz > last_size:
                with LOG_PATH.open("rb") as f:
                    f.seek(last_size)
                    sys.stdout.write(f.read().decode("utf-8", errors="replace"))
                last_size = sz
        time.sleep(1.5)
    raise SystemExit("Daemon did not become healthy in time")


def wait_for_prewarm(timeout_s: int = 300) -> None:
    banner("Wait for default bundle prewarm")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.post(
                f"http://127.0.0.1:{PORT}/sessions",
                json={"bundle_name": BUNDLE_NAME, "working_dir": "/tmp"},
                timeout=10.0,
            )
            if r.status_code in (200, 201):
                data = r.json()
                sid = data["session_id"]
                # Tear it down — we just wanted to confirm prewarm is done
                httpx.delete(f"http://127.0.0.1:{PORT}/sessions/{sid}", timeout=5.0)
                print(f"  ✓ prewarm complete (probe session {sid[:24]}... created & cleaned up)", flush=True)
                return
            if r.status_code == 503:
                ra = float(r.headers.get("Retry-After", "5"))
                print(f"  ··· prewarm in progress, retry in {ra}s", flush=True)
                time.sleep(ra)
                continue
            r.raise_for_status()
        except httpx.HTTPError as e:
            print(f"  · transient: {e}", flush=True)
            time.sleep(2.0)
    raise SystemExit("Prewarm did not finish")


def run_bun_smoke() -> int:
    banner("Run Bun smoke test")
    env = os.environ.copy()
    env["AMPLIFIERD_URL"] = f"http://127.0.0.1:{PORT}"
    env["AMPLIFIER_DEFAULT_BUNDLE"] = BUNDLE_NAME
    env["AMPLIFIER_TURN_TIMEOUT_MS"] = "600000"
    env["AMPLIFIER_HEARTBEAT_MS"] = "3000"
    rc = subprocess.run(
        [str(BUN_BIN), "run", str(SMOKE_TS)],
        env=env,
        cwd=str(REPO / "nanoclaw-provider"),
    ).returncode
    return rc


def stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    banner("Tear down amplifierd")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY required", file=sys.stderr); return 1
    for p in [AMP_BIN, BUN_BIN, BUNDLE_PATH, SMOKE_TS]:
        if not p.exists():
            print(f"Missing: {p}", file=sys.stderr); return 1
    proc = start_daemon()
    try:
        wait_healthy()
        wait_for_prewarm()
        rc = run_bun_smoke()
        return rc
    finally:
        stop_daemon(proc)


if __name__ == "__main__":
    sys.exit(main())
