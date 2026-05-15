"""amp-claw — manage the Amplifier brain behind nanoclaw.

Lives at ~/.nanoclaw-amp/bin/amp-claw after install. All state under
~/.nanoclaw-amp/. Never reads or writes ~/.amplifier/ or ~/.claude/.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx
import yaml

HOME = Path.home()
ROOT = HOME / ".nanoclaw-amp"
CONFIG_PATH = ROOT / "config.yaml"
KEYS_PATH = ROOT / "keys.env"
LOG_PATH = ROOT / "logs" / "amplifierd.log"
PID_PATH = ROOT / "amplifierd.pid"
BUNDLE_DIR = ROOT / "bundles"
DATA_DIR = ROOT / "data"
AMPD_BIN = ROOT / "bin" / "amplifierd"

# Map backend → provider-module config block for the bundle YAML.
# Keep in sync with amplifier-module-provider-*.
BACKENDS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "module": "provider-anthropic",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-anthropic@main",
        "default_model": "claude-sonnet-4-5",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "module": "provider-openai",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-openai@main",
        "default_model": "gpt-5",
        "key_env": "OPENAI_API_KEY",
    },
    "openai-chatgpt": {
        "module": "provider-openai-chatgpt",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-openai-chatgpt@main",
        "default_model": "gpt-5-codex",
        "key_env": None,  # OAuth via Codex CLI
    },
    "azure-openai": {
        "module": "provider-azure-openai",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-azure-openai@main",
        "default_model": "gpt-4o",
        "key_env": "AZURE_OPENAI_API_KEY",
    },
    "gemini": {
        "module": "provider-gemini",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-gemini@main",
        "default_model": "gemini-2.0-flash",
        "key_env": "GEMINI_API_KEY",
    },
    "chat-completions": {
        "module": "provider-chat-completions",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-chat-completions@main",
        "default_model": "llama-3.1-70b",
        "key_env": None,  # configured per endpoint
    },
    "ollama": {
        "module": "provider-ollama",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-ollama@main",
        "default_model": "llama3.1:70b",
        "key_env": None,
    },
    "vllm": {
        "module": "provider-vllm",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-vllm@main",
        "default_model": "meta-llama/Meta-Llama-3.1-70B",
        "key_env": None,
    },
    "copilot": {
        "module": "provider-github-copilot",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-github-copilot@main",
        "default_model": "gpt-4o",
        "key_env": "GITHUB_TOKEN",
    },
    "mock": {
        "module": "provider-mock",
        "source": "git+https://github.com/microsoft/amplifier-module-provider-mock@main",
        "default_model": "mock-model",
        "key_env": None,
    },
}


# ---------- config helpers ----------

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(CONFIG_PATH.read_text()) or {}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False))


def load_keys() -> dict[str, str]:
    if not KEYS_PATH.exists():
        return {}
    out = {}
    for line in KEYS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def save_keys(keys: dict[str, str]) -> None:
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in keys.items()]
    KEYS_PATH.write_text("\n".join(lines) + "\n")
    KEYS_PATH.chmod(0o600)


def write_bundle(backend: str, model: str | None = None, endpoint: str | None = None) -> Path:
    """Compose the build-up + chosen backend + tool-mcp bundle file."""
    if backend not in BACKENDS:
        raise click.UsageError(f"Unknown backend '{backend}'. Try `amp-claw backend list`.")
    b = BACKENDS[backend]
    used_model = model or b["default_model"]
    provider_config: dict[str, Any] = {"default_model": used_model}
    if endpoint:
        provider_config["endpoint"] = endpoint
    # Indent each line of the dumped YAML to nest under `config:` (6 spaces).
    pc_yaml = yaml.safe_dump(provider_config, indent=2, default_flow_style=False).rstrip()
    pc_indented = "\n".join("      " + line for line in pc_yaml.splitlines())

    bundle_content = f"""---
bundle:
  name: nanoclaw-amp
  version: 0.1.0
  description: build-up bundle + {backend} backend + nanoclaw MCP bridge

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=experiments/build-up/build-up-foundation.md

providers:
  - module: {b["module"]}
    source: {b["source"]}
    config:
{pc_indented}

tools:
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
    config:
      servers:
        nanoclaw:
          command: bun
          args: ["run", "/app/src/mcp-tools/index.ts"]
          transport: stdio
          env: {{}}
---

# nanoclaw output contract

Every outbound user-facing message **must** be wrapped in
`<message to="name">...</message>` blocks. Unwrapped text is treated as
internal scratchpad and not delivered to any channel. Destination names
are bound at session start.
"""
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    out = BUNDLE_DIR / "nanoclaw-amp.md"
    out.write_text(bundle_content)
    return out


# ---------- amplifierd lifecycle ----------

def _amp_url() -> str:
    cfg = load_config()
    # Probe URL is always loopback for local control, even when amplifierd
    # binds 0.0.0.0 for external (docker bridge) clients.
    return f"http://127.0.0.1:{cfg.get('port', 8410)}"


def _amp_bind_host() -> str:
    """Where amplifierd binds for inbound traffic. Defaults to 127.0.0.1 for
    isolation; set to 0.0.0.0 (or a specific iface) when the agent runs in a
    sibling container that reaches us via host.docker.internal or a bridge."""
    cfg = load_config()
    return cfg.get("bind_host", "127.0.0.1")


def is_running() -> bool:
    try:
        r = httpx.get(f"{_amp_url()}/health", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def start_daemon() -> int:
    cfg = load_config()
    port = cfg.get("port", 8410)
    bundle_path = BUNDLE_DIR / "nanoclaw-amp.md"
    if not bundle_path.exists():
        click.echo("✗ No bundle. Run `amp-claw backend set <name>` first.", err=True)
        return 1
    if is_running():
        click.echo(f"✓ Already running on :{port}")
        return 0
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(load_keys())
    env["PYTHONUNBUFFERED"] = "1"
    env["AMPLIFIERD_HOME_DIR"] = str(DATA_DIR)
    env["AMPLIFIERD_PROJECTS_DIR"] = str(DATA_DIR / "projects")
    cmd = [
        str(AMPD_BIN), "serve",
        "--host", _amp_bind_host(),
        "--port", str(port),
        "--bundle", f"nanoclaw-amp=file://{bundle_path}",
        "--default-bundle", "nanoclaw-amp",
        "--log-level", cfg.get("log_level", "info"),
    ]
    proc = subprocess.Popen(
        cmd, stdout=LOG_PATH.open("ab"), stderr=subprocess.STDOUT,
        env=env, start_new_session=True,
    )
    PID_PATH.write_text(str(proc.pid))
    click.echo(f"  Started amplifierd PID {proc.pid} on :{port}")
    # brief wait
    for _ in range(60):
        if is_running():
            click.echo(f"  ✓ healthy")
            return 0
        time.sleep(1)
    click.echo("  ⚠ daemon did not become healthy in 60s; check logs", err=True)
    return 1


def stop_daemon() -> None:
    if not PID_PATH.exists():
        return
    try:
        pid = int(PID_PATH.read_text().strip())
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        for _ in range(15):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    PID_PATH.unlink(missing_ok=True)


# ---------- CLI ----------

@click.group()
@click.version_option()
def main() -> None:
    """amp-claw — Amplifier brain behind nanoclaw."""
    ROOT.mkdir(parents=True, exist_ok=True)


@main.group()
def backend() -> None:
    """Pick which Amplifier backend drives the agent."""


@backend.command("list")
def backend_list() -> None:
    cfg = load_config()
    current = cfg.get("backend", "(none)")
    click.echo(f"Current backend: {current}\n")
    click.echo("Available backends:")
    for name, b in BACKENDS.items():
        marker = " ←" if name == current else ""
        key = f" (env: {b['key_env']})" if b["key_env"] else ""
        click.echo(f"  {name:<18} {b['module']}{key}{marker}")


@backend.command("set")
@click.argument("name")
@click.option("--model", help="Override the backend's default model")
@click.option("--endpoint", help="Endpoint URL (for chat-completions, vllm, ollama)")
@click.option("--port", type=int, help="amplifierd port (default 8410)")
@click.option("--bind-host", help="amplifierd bind address. Default 127.0.0.1. Use 0.0.0.0 when nanoclaw containers reach us via host.docker.internal.")
def backend_set(name: str, model: str | None, endpoint: str | None,
                port: int | None, bind_host: str | None) -> None:
    if name not in BACKENDS:
        click.echo(f"✗ Unknown backend '{name}'. Try `amp-claw backend list`.", err=True)
        sys.exit(1)
    cfg = load_config()
    cfg["backend"] = name
    cfg["model"] = model or BACKENDS[name]["default_model"]
    if endpoint: cfg["endpoint"] = endpoint
    if port: cfg["port"] = port
    if bind_host: cfg["bind_host"] = bind_host
    cfg.setdefault("port", 8410)
    cfg.setdefault("bind_host", "127.0.0.1")
    save_config(cfg)
    bundle_path = write_bundle(name, model, endpoint)
    click.echo(f"✓ Backend set to {name} (model={cfg['model']})")
    click.echo(f"✓ Bundle written to {bundle_path}")
    # Restart daemon if running so the new bundle takes effect
    if is_running():
        click.echo("  Restarting amplifierd to pick up new bundle...")
        stop_daemon()
        start_daemon()


@main.group()
def key() -> None:
    """Manage backend API keys (stored at ~/.nanoclaw-amp/keys.env)."""


@key.command("set")
@click.argument("backend_name")
@click.argument("value", required=False)
def key_set(backend_name: str, value: str | None) -> None:
    if backend_name not in BACKENDS:
        click.echo(f"✗ Unknown backend '{backend_name}'", err=True); sys.exit(1)
    env_name = BACKENDS[backend_name]["key_env"]
    if not env_name:
        click.echo(f"  {backend_name} does not use a single API key (OAuth or endpoint-configured).")
        return
    if value is None:
        value = click.prompt(f"  {env_name}", hide_input=True)
    keys = load_keys()
    keys[env_name] = value
    save_keys(keys)
    click.echo(f"✓ Stored {env_name} in {KEYS_PATH}")


@key.command("list")
def key_list() -> None:
    keys = load_keys()
    if not keys:
        click.echo("(no keys stored)")
        return
    for k, v in keys.items():
        click.echo(f"  {k:<25} {v[:4]}…{v[-6:] if len(v) > 10 else ''} ({len(v)} chars)")


@main.command()
def status() -> None:
    cfg = load_config()
    if not cfg:
        click.echo("not configured. Run `amp-claw backend set anthropic`."); return
    click.echo(f"Backend:   {cfg.get('backend')} / {cfg.get('model')}")
    click.echo(f"Bundle:    {BUNDLE_DIR / 'nanoclaw-amp.md'}")
    click.echo(f"Port:      {cfg.get('port', 8410)}")
    if is_running():
        try:
            r = httpx.get(f"{_amp_url()}/health", timeout=2.0)
            info = r.json()
            click.echo(f"Daemon:    ✓ running, uptime {info.get('uptime_seconds', 0):.0f}s, sessions={info.get('active_sessions', 0)}")
        except Exception:
            click.echo("Daemon:    ✓ running (no health JSON)")
    else:
        click.echo("Daemon:    ✗ not running. `amp-claw restart`")


@main.command()
def restart() -> None:
    stop_daemon()
    sys.exit(start_daemon())


@main.command()
def stop() -> None:
    stop_daemon()
    click.echo("✓ Stopped")


@main.command()
@click.option("--follow", "-f", is_flag=True, help="Tail logs (Ctrl-C to stop)")
@click.option("--lines", "-n", default=80, type=int)
def logs(follow: bool, lines: int) -> None:
    if not LOG_PATH.exists():
        click.echo("(no logs yet)"); return
    if follow:
        subprocess.call(["tail", "-f", "-n", str(lines), str(LOG_PATH)])
    else:
        subprocess.call(["tail", "-n", str(lines), str(LOG_PATH)])


@main.command()
def doctor() -> None:
    """End-to-end health probe."""
    ok = True
    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            msg = fn()
            click.echo(f"  ✓ {name}: {msg}")
        except Exception as e:
            click.echo(f"  ✗ {name}: {e}")
            ok = False

    cfg = load_config()
    check("Config",    lambda: f"backend={cfg.get('backend')}, model={cfg.get('model')}, port={cfg.get('port')}")
    check("Bundle",    lambda: f"exists ({(BUNDLE_DIR / 'nanoclaw-amp.md').stat().st_size} bytes)")
    check("Daemon",    lambda: (httpx.get(f"{_amp_url()}/health", timeout=2).text)[:60] if is_running() else (_ for _ in ()).throw(RuntimeError("not running — run `amp-claw restart`")))
    if is_running():
        check("Round-trip",
              lambda: _do_round_trip())

    if not ok:
        sys.exit(1)


def _do_round_trip() -> str:
    """Create a probe session, run two turns to prove multi-turn context,
    delete cleanly. Real LLM calls — beyond trivial first-turn validation."""
    base = _amp_url()
    # Tolerate 503 prewarm — bundle prepare can take ~45s on first call
    sid = None
    for i in range(60):
        r = httpx.post(f"{base}/sessions",
                       json={"bundle_name": "nanoclaw-amp", "working_dir": "/tmp"},
                       timeout=30)
        if r.status_code == 503:
            time.sleep(float(r.headers.get("Retry-After", 5))); continue
        r.raise_for_status()
        sid = r.json()["session_id"]
        break
    if sid is None:
        raise RuntimeError("session create did not succeed in 60 attempts")
    try:
        # ----- Turn 1: trivial math -----
        r1 = httpx.post(f"{base}/sessions/{sid}/execute",
                        json={"prompt": "What is 13 times 17? Reply with just the number, nothing else."},
                        timeout=300)
        r1.raise_for_status()
        resp1 = (r1.json().get("response") or "").strip()
        if "221" not in resp1:
            raise RuntimeError(f"Turn 1 failed: expected '221' in response, got {resp1[:200]!r}")
        # ----- Turn 2: prove multi-turn context -----
        r2 = httpx.post(f"{base}/sessions/{sid}/execute",
                        json={"prompt": "Divide that number by 13. Reply with just the number."},
                        timeout=300)
        r2.raise_for_status()
        resp2 = (r2.json().get("response") or "").strip()
        if "17" not in resp2:
            raise RuntimeError(f"Turn 2 (multi-turn context) failed: expected '17', got {resp2[:200]!r}")
        return f"13×17=221, 221÷13=17 (PASS — multi-turn context proven)"
    finally:
        try: httpx.delete(f"{base}/sessions/{sid}", timeout=5)
        except Exception: pass


@main.command("service")
@click.argument("action", type=click.Choice(["install", "uninstall"]))
def service(action: str) -> None:
    """Install/uninstall amplifierd as a system service."""
    if sys.platform == "linux":
        unit = HOME / ".config/systemd/user/nanoclaw-amp.service"
        if action == "install":
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(f"""[Unit]
Description=amplifierd (nanoclaw-on-amplifier brain)
After=network.target

[Service]
ExecStart={AMPD_BIN} serve --host {load_config().get('bind_host', '127.0.0.1')} --port {load_config().get('port', 8410)} --bundle nanoclaw-amp=file://{BUNDLE_DIR}/nanoclaw-amp.md --default-bundle nanoclaw-amp
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-{KEYS_PATH}
Environment=AMPLIFIERD_HOME_DIR={DATA_DIR}
Environment=AMPLIFIERD_PROJECTS_DIR={DATA_DIR}/projects
Restart=on-failure

[Install]
WantedBy=default.target
""")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "--now", "nanoclaw-amp.service"], check=False)
            click.echo(f"✓ systemd unit installed at {unit}")
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", "nanoclaw-amp.service"], check=False)
            unit.unlink(missing_ok=True)
            click.echo("✓ systemd unit removed")
    elif sys.platform == "darwin":
        plist = HOME / "Library/LaunchAgents/com.nanoclaw-amp.plist"
        if action == "install":
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nanoclaw-amp</string>
  <key>ProgramArguments</key>
  <array>
    <string>{AMPD_BIN}</string>
    <string>serve</string>
    <string>--port</string><string>{load_config().get('port', 8410)}</string>
    <string>--bundle</string><string>nanoclaw-amp=file://{BUNDLE_DIR}/nanoclaw-amp.md</string>
    <string>--default-bundle</string><string>nanoclaw-amp</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AMPLIFIERD_HOME_DIR</key><string>{DATA_DIR}</string>
    <key>AMPLIFIERD_PROJECTS_DIR</key><string>{DATA_DIR}/projects</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>""")
            subprocess.run(["launchctl", "load", str(plist)], check=False)
            click.echo(f"✓ launchd plist installed at {plist}")
        else:
            subprocess.run(["launchctl", "unload", str(plist)], check=False)
            plist.unlink(missing_ok=True)
            click.echo("✓ launchd plist removed")
    else:
        click.echo(f"⚠ Unsupported platform: {sys.platform}. Run `amp-claw restart` manually.", err=True)


if __name__ == "__main__":
    main()
