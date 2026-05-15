#!/usr/bin/env python3
"""POC-A: Smoke-test amplifierd + build-up bundle + provider-anthropic.

Spawns amplifierd as a subprocess against our local bundle, polls /health,
creates a session, runs a trivial first turn and a multi-turn follow-up,
then tears down. Single script — no background bash juggling.

Validates:
  * Daemon comes up cleanly with our bundle (build-up + provider-anthropic).
  * `POST /sessions` creates an AmplifierSession.
  * `POST /sessions/{id}/execute` returns a real reply.
  * Multi-turn: second `execute` on the same session_id picks up context.
  * Cleanup via `DELETE /sessions/{id}`.

Run:
  ANTHROPIC_API_KEY=sk-... python3 poc/smoke_test.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

# ---------- Config ----------
PORT = 18410
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
SESSIONS_URL = f"http://127.0.0.1:{PORT}/sessions"
HOME = Path.home()
AMP_BIN = HOME / ".nanoclaw-amp" / "bin" / "amplifierd"
BUNDLE_PATH = Path(__file__).parent / "bundle.local.md"
BUNDLE_NAME = "nanoclaw-amp-local"
LOG_PATH = Path(__file__).parent / ".amplifierd.log"
WORKDIR = Path("/tmp/amp-claw-poc-workdir")
WORKDIR.mkdir(exist_ok=True)

# ---------- Pretty print ----------
def banner(text: str) -> None:
    print(f"\n{'=' * 6} {text} {'=' * (60 - len(text))}", flush=True)

def step(text: str) -> None:
    print(f"  → {text}", flush=True)

def ok(text: str) -> None:
    print(f"  ✓ {text}", flush=True)

def fail(text: str) -> None:
    print(f"  ✗ {text}", flush=True)


# ---------- Daemon lifecycle ----------
def start_daemon() -> subprocess.Popen[bytes]:
    banner(f"Starting amplifierd on :{PORT}")
    step(f"Bundle: {BUNDLE_NAME} -> {BUNDLE_PATH}")
    step(f"Log: {LOG_PATH}")
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
    log_file = LOG_PATH.open("wb")
    proc = subprocess.Popen(
        cmd, stdout=log_file, stderr=subprocess.STDOUT, env=env,
        start_new_session=True,
    )
    step(f"PID: {proc.pid}")
    return proc


def wait_for_health(timeout_s: int = 180) -> None:
    banner("Waiting for /health (up to %ds)" % timeout_s)
    deadline = time.monotonic() + timeout_s
    last_log_size = 0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(HEALTH_URL, timeout=2.0)
            if r.status_code == 200:
                ok(f"Daemon healthy: {r.text[:80]}")
                return
        except (httpx.RequestError, httpx.HTTPError):
            pass
        # Show log progress while we wait
        if LOG_PATH.exists():
            sz = LOG_PATH.stat().st_size
            if sz > last_log_size:
                with LOG_PATH.open("rb") as f:
                    f.seek(last_log_size)
                    chunk = f.read()
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
                last_log_size = sz
        time.sleep(1.5)
    fail("Daemon did not become healthy in time")
    raise SystemExit(2)


def stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    banner("Stopping amplifierd")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    step("Stopped")


# ---------- API calls ----------
def create_session(working_dir: str = str(WORKDIR), prewarm_timeout_s: int = 600) -> str:
    banner("Create session")
    deadline = time.monotonic() + prewarm_timeout_s
    attempt = 0
    while True:
        attempt += 1
        r = httpx.post(
            SESSIONS_URL,
            json={"bundle_name": BUNDLE_NAME, "working_dir": working_dir},
            timeout=600.0,
        )
        if r.status_code == 503:
            # Per amplifierd contract: prewarm-in-progress. Use Retry-After hint, retry.
            retry_after = float(r.headers.get("Retry-After", "5"))
            elapsed = int(time.monotonic() - (deadline - prewarm_timeout_s))
            step(f"[attempt {attempt}, {elapsed}s elapsed] 503 prewarm in progress → retry in {retry_after}s")
            if time.monotonic() + retry_after > deadline:
                fail(f"Bundle prewarm did not complete within {prewarm_timeout_s}s")
                raise SystemExit(2)
            time.sleep(retry_after)
            continue
        r.raise_for_status()
        data = r.json()
        sid = data["session_id"]
        ok(f"session_id={sid}")
        step(f"status={data.get('status')}  bundle={data.get('bundle_name')}  cwd={data.get('working_dir')}")
        return sid


def execute(session_id: str, prompt: str, timeout_s: int = 300) -> str:
    step(f"prompt: {prompt!r}")
    r = httpx.post(
        f"{SESSIONS_URL}/{session_id}/execute",
        json={"prompt": prompt},
        timeout=timeout_s,
    )
    r.raise_for_status()
    data = r.json()
    response = data.get("response") or ""
    ok(f"response ({len(response)} chars):")
    for line in response.splitlines()[:20]:
        print(f"      {line}", flush=True)
    if len(response.splitlines()) > 20:
        print(f"      ... [{len(response.splitlines()) - 20} more lines]", flush=True)
    return response


def get_session_detail(session_id: str) -> dict:
    r = httpx.get(f"{SESSIONS_URL}/{session_id}", timeout=5.0)
    r.raise_for_status()
    return r.json()


def delete_session(session_id: str) -> None:
    httpx.delete(f"{SESSIONS_URL}/{session_id}", timeout=10.0)


# ---------- Main flow ----------
def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        fail("ANTHROPIC_API_KEY is required in env")
        return 1
    if not AMP_BIN.exists():
        fail(f"amplifierd binary not found at {AMP_BIN}")
        return 1
    if not BUNDLE_PATH.exists():
        fail(f"bundle not found at {BUNDLE_PATH}")
        return 1

    proc = start_daemon()
    try:
        wait_for_health()

        # ---- Turn 1: trivial ----
        banner("Turn 1 — trivial math")
        sid = create_session()
        r1 = execute(sid, "What is 13 times 17? Answer with just the number, no other text.", timeout_s=300)
        if "221" not in r1:
            fail(f"Turn 1 did not contain '221'. Full response: {r1!r}")
            return 3
        ok("Turn 1 passed (contains '221')")

        # ---- Turn 2: multi-turn context ----
        banner("Turn 2 — multi-turn context (same session)")
        r2 = execute(sid, "Now divide that number by 13. Answer with just the number.", timeout_s=300)
        if "17" not in r2:
            fail(f"Turn 2 did not contain '17'. Full response: {r2!r}")
            return 4
        ok("Turn 2 passed (contains '17' — proves context carries across turns)")

        # ---- Turn 3: explicit delegation request (tests build-up agent dispatch) ----
        banner("Turn 3 — delegation request (tests build-up explorer agent)")
        r3 = execute(
            sid,
            "Use your explorer agent to list the names of the directories at /tmp/amp-claw-poc-workdir. "
            "Reply with just the directory names, one per line.",
            timeout_s=600,
        )
        # We don't strictly assert the content (it's empty dir) — just that it didn't crash and produced a reply
        if r3 is None or r3.strip() == "":
            fail("Turn 3 produced an empty response — delegation may have failed")
            return 5
        ok(f"Turn 3 produced a {len(r3)}-char reply")

        # ---- Inspect session detail ----
        banner("Session detail")
        d = get_session_detail(sid)
        ok(f"status={d.get('status')} mounted_modules={d.get('mounted_modules')}")
        print(f"      capabilities={d.get('capabilities')}", flush=True)

        # ---- Clean up ----
        delete_session(sid)
        ok("Session deleted")

        banner("✅ POC-A: all three turns passed")
        return 0

    finally:
        stop_daemon(proc)


if __name__ == "__main__":
    sys.exit(main())
