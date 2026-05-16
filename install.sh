#!/usr/bin/env bash
# nanoclaw-on-amplifier installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/bkrabach/nanoclaw-on-amplifier/main/install.sh | bash
#
# Or, after cloning the repo:
#   bash install.sh [--skip-nanoclaw] [--backend anthropic] [--port 8410] [--reconfigure]
#
# Wraps nanoclaw's own install, then wedges in our Amplifier provider.

set -euo pipefail

# Resolve our own absolute directory BEFORE any cd. install.sh may be invoked
# as `bash install.sh` (where $0='install.sh' and dirname is '.'). We need
# the absolute repo root so cp commands later don't lose context after
# `cd "$NANOCLAW_DIR"`.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || pwd)"

# ---------- Args ----------
SKIP_NANOCLAW=0
RECONFIGURE=0
AMPLIFIERD_ONLY=0
BIND_HOST="127.0.0.1"
BACKEND=""
PORT="8410"
NANOCLAW_DIR="${NANOCLAW_DIR:-$HOME/nanoclaw}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-nanoclaw)    SKIP_NANOCLAW=1; shift ;;
    --amplifierd-only)  AMPLIFIERD_ONLY=1; SKIP_NANOCLAW=1; shift ;;
    --reconfigure)      RECONFIGURE=1; shift ;;
    --backend)          BACKEND="$2"; shift 2 ;;
    --port)             PORT="$2"; shift 2 ;;
    --bind-host)        BIND_HOST="$2"; shift 2 ;;
    --nanoclaw-dir)     NANOCLAW_DIR="$2"; shift 2 ;;
    -h|--help)
      head -15 "$0" | tail -12
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---------- Pretty ----------
say()  { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m⚠\033[0m %s\n" "$*"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$*"; exit 1; }
ask()  { read -p "$1" -r REPLY < /dev/tty; }

# ---------- Bootstrap deps ----------
say "Checking prerequisites"

# uv (we install amplifierd via uv)
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found — installing via official one-liner"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null || fail "uv install failed"
ok "uv $(uv --version | awk '{print $2}')"

# Docker (nanoclaw needs it for per-session agent containers; we skip the
# check when --skip-nanoclaw is set so the installer can run in environments
# that only need amplifierd + amp-claw, e.g. DTU profiles or remote daemon
# hosts)
if [[ $SKIP_NANOCLAW -eq 0 ]]; then
  command -v docker >/dev/null || fail "Docker required for nanoclaw (https://docs.docker.com/get-docker/)"
  ok "docker $(docker --version | head -1)"
else
  command -v docker >/dev/null 2>&1 && ok "docker $(docker --version | head -1)" || warn "docker missing (OK with --skip-nanoclaw)"
fi

# Node/pnpm/bun — nanoclaw bootstraps these itself when we run its installer,
# but if they exist we save some time.
for tool in node pnpm bun; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool $(${tool} --version 2>&1 | head -1)"; fi
done

# gh CLI is optional (only needed if user wants private-repo skills)
command -v gh >/dev/null 2>&1 && ok "gh $(gh --version | head -1 | awk '{print $3}')" || warn "gh not found (optional)"

# ---------- Run nanoclaw's installer (unless skipped) ----------
if [[ $SKIP_NANOCLAW -eq 0 ]]; then
  if [[ -d "$NANOCLAW_DIR/.git" ]]; then
    say "Existing nanoclaw checkout at $NANOCLAW_DIR — leaving it alone"
  else
    say "Cloning nanoclaw into $NANOCLAW_DIR"
    git clone https://github.com/nanocoai/nanoclaw.git "$NANOCLAW_DIR"
  fi
  cd "$NANOCLAW_DIR"

  # Skip nanoclaw's auth step since we're providing our own brain.
  # The cli-agent step would ping Claude Code, which we replace.
  say "Running nanoclaw's installer with auth+cli-agent skipped"
  NANOCLAW_SKIP=auth,cli-agent bash nanoclaw.sh || warn "nanoclaw setup exited non-zero (may be OK if just stopped early)"
fi

if [[ $AMPLIFIERD_ONLY -eq 0 ]]; then
  cd "$NANOCLAW_DIR"
  [[ -f pnpm-workspace.yaml && -f src/db/schema.ts ]] || fail "Not a nanoclaw v2 checkout at $NANOCLAW_DIR"
  ok "nanoclaw v2 detected at $NANOCLAW_DIR"
fi

# ---------- Install amplifierd into isolated namespace ----------
say "Installing amplifierd into ~/.nanoclaw-amp/ (isolated namespace)"

mkdir -p "$HOME/.nanoclaw-amp"/{bin,data,data/cache,data/sessions,data/projects,bundles,logs}

if [[ ! -x "$HOME/.nanoclaw-amp/bin/amplifierd" ]]; then
  uv venv --python 3.12 "$HOME/.nanoclaw-amp/venv"
  VIRTUAL_ENV="$HOME/.nanoclaw-amp/venv" uv pip install \
    "git+https://github.com/microsoft/amplifierd@main" \
    httpx click pyyaml >/dev/null
  cat > "$HOME/.nanoclaw-amp/bin/amplifierd" <<'SHIM'
#!/usr/bin/env bash
exec "$HOME/.nanoclaw-amp/venv/bin/amplifierd" "$@"
SHIM
  chmod +x "$HOME/.nanoclaw-amp/bin/amplifierd"
fi
ok "amplifierd installed at ~/.nanoclaw-amp/bin/amplifierd"

# ---------- Install amp-claw CLI ----------
say "Installing amp-claw CLI"
# In-tree development install: use this repo if invoked from within it.
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]] && grep -q "name = \"nanoclaw-on-amplifier\"" "$SCRIPT_DIR/pyproject.toml"; then
  VIRTUAL_ENV="$HOME/.nanoclaw-amp/venv" uv pip install -e "$SCRIPT_DIR" >/dev/null
else
  VIRTUAL_ENV="$HOME/.nanoclaw-amp/venv" uv pip install \
    "git+https://github.com/bkrabach/nanoclaw-on-amplifier@main" >/dev/null
fi
ln -sf "$HOME/.nanoclaw-amp/venv/bin/amp-claw" "$HOME/.nanoclaw-amp/bin/amp-claw"
ok "amp-claw at ~/.nanoclaw-amp/bin/amp-claw"

# Ensure ~/.nanoclaw-amp/bin is on PATH for interactive shells.
if ! echo ":$PATH:" | grep -q ":$HOME/.nanoclaw-amp/bin:"; then
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" ]] || continue
    grep -qF ".nanoclaw-amp/bin" "$rc" || \
      echo 'export PATH="$HOME/.nanoclaw-amp/bin:$PATH"' >> "$rc"
  done
fi

# Also expose the binaries at /usr/local/bin so they work in non-interactive
# contexts: system services (systemd/launchd), DTU `incus exec`, scripts that
# don't source any rc file. We attempt this with sudo if /usr/local/bin is
# not writable; fall back to ~/.local/bin (which most distros put on PATH
# via /etc/profile.d).
LINK_DEST=""
if [[ -w /usr/local/bin ]] 2>/dev/null; then
  LINK_DEST=/usr/local/bin
elif sudo -n true 2>/dev/null && sudo test -w /usr/local/bin; then
  LINK_DEST=/usr/local/bin
elif [[ -d "$HOME/.local/bin" ]] || mkdir -p "$HOME/.local/bin"; then
  LINK_DEST="$HOME/.local/bin"
fi
if [[ -n "$LINK_DEST" ]]; then
  if [[ "$LINK_DEST" == /usr/local/bin && ! -w "$LINK_DEST" ]]; then
    sudo ln -sf "$HOME/.nanoclaw-amp/bin/amp-claw"   "$LINK_DEST/amp-claw"
    sudo ln -sf "$HOME/.nanoclaw-amp/bin/amplifierd" "$LINK_DEST/amplifierd"
  else
    ln -sf "$HOME/.nanoclaw-amp/bin/amp-claw"   "$LINK_DEST/amp-claw"
    ln -sf "$HOME/.nanoclaw-amp/bin/amplifierd" "$LINK_DEST/amplifierd"
  fi
  ok "Linked binaries into $LINK_DEST (visible to all shells + services)"
else
  warn "Couldn't link into /usr/local/bin or ~/.local/bin; rely on rc-file PATH only"
fi

# ---------- Pick a backend ----------
if [[ -z "$BACKEND" ]] && [[ ! -f "$HOME/.nanoclaw-amp/config.yaml" || $RECONFIGURE -eq 1 ]]; then
  cat <<MENU

Which Amplifier backend should drive your nanoclaw?
  1) anthropic         (claude-sonnet-4-5; recommended)
  2) openai            (gpt-4 / gpt-5)
  3) openai-chatgpt    (ChatGPT subscription via Codex OAuth — no API billing)
  4) azure-openai
  5) gemini            (1M context, Google)
  6) chat-completions  (llama.cpp / vLLM / LM Studio / LocalAI)
  7) ollama
  8) vllm
  9) copilot           (GitHub Copilot models)
 10) mock              (tests, no real LLM)

MENU
  ask "Choice [1]: "
  case "${REPLY:-1}" in
    1)  BACKEND=anthropic ;;
    2)  BACKEND=openai ;;
    3)  BACKEND=openai-chatgpt ;;
    4)  BACKEND=azure-openai ;;
    5)  BACKEND=gemini ;;
    6)  BACKEND=chat-completions ;;
    7)  BACKEND=ollama ;;
    8)  BACKEND=vllm ;;
    9)  BACKEND=copilot ;;
    10) BACKEND=mock ;;
    *)  BACKEND=anthropic ;;
  esac
fi

if [[ -n "$BACKEND" ]]; then
  "$HOME/.nanoclaw-amp/bin/amp-claw" backend set "$BACKEND" --port "$PORT" --bind-host "$BIND_HOST"
fi

# ---------- Wedge the provider files into nanoclaw ----------
if [[ $AMPLIFIERD_ONLY -eq 1 ]]; then
  say "Skipping nanoclaw wedge (--amplifierd-only)"
  # Start amplifierd as a background process (not a system service — appropriate
  # for DTUs and other ephemeral or non-systemd contexts). The non-amplifierd-only
  # path calls `amp-claw service install` instead.
  "$HOME/.nanoclaw-amp/bin/amp-claw" restart
  "$HOME/.nanoclaw-amp/bin/amp-claw" doctor || warn "doctor reported issues; check logs"
  exit 0
fi

say "Wedging amplifier provider into $NANOCLAW_DIR"

# Locate the provider files. Prefer the local checkout (SCRIPT_DIR) over
# fetching from GitHub. The curl-pipe path lands as a single file, so we
# fetch into a temp dir laid out the same way as the repo.
PROVIDER_SRC=""
HOST_PROVIDER_SRC=""
if [[ -f "$SCRIPT_DIR/nanoclaw-provider/amplifier.ts" ]]; then
  PROVIDER_SRC="$SCRIPT_DIR/nanoclaw-provider/amplifier.ts"
  HOST_PROVIDER_SRC="$SCRIPT_DIR/nanoclaw-skill/add-amplifier/host-amplifier.ts"
else
  TMP=$(mktemp -d)
  curl -fsSL "https://raw.githubusercontent.com/bkrabach/nanoclaw-on-amplifier/main/nanoclaw-provider/amplifier.ts" \
    -o "$TMP/amplifier.ts"
  curl -fsSL "https://raw.githubusercontent.com/bkrabach/nanoclaw-on-amplifier/main/nanoclaw-skill/add-amplifier/host-amplifier.ts" \
    -o "$TMP/host-amplifier.ts"
  PROVIDER_SRC="$TMP/amplifier.ts"
  HOST_PROVIDER_SRC="$TMP/host-amplifier.ts"
fi
cp "$PROVIDER_SRC" "$NANOCLAW_DIR/container/agent-runner/src/providers/amplifier.ts"
if [[ -f "$HOST_PROVIDER_SRC" ]]; then
  cp "$HOST_PROVIDER_SRC" "$NANOCLAW_DIR/src/providers/amplifier.ts"
fi
ok "Provider files dropped"

# Append barrels (idempotent)
for f in "$NANOCLAW_DIR/src/providers/index.ts" "$NANOCLAW_DIR/container/agent-runner/src/providers/index.ts"; do
  LINE="import './amplifier.js';"
  if [[ -f "$f" ]]; then
    grep -qxF "$LINE" "$f" || echo "$LINE" >> "$f"
    ok "Updated $(basename "$(dirname "$f")")/index.ts"
  fi
done

# Patch container-runner.ts to skip OneCLI gateway when provider=amplifier.
#
# Background: nanoclaw's container-runner.ts unconditionally invokes the
# OneCLI gateway (~app.onecli.sh) on every container spawn for credential
# injection. The `amplifier` provider doesn't use OneCLI — amplifierd runs
# on the host and the container reaches it via plain HTTP with NO_PROXY
# bypass. Without this patch a fresh user with no OneCLI account gets a
# hard 401 on every message they send. Idempotent (the helper detects an
# already-applied marker and no-ops).
PATCH_SCRIPT="$SCRIPT_DIR/scripts/apply-onecli-bypass.py"
if [[ ! -f "$PATCH_SCRIPT" ]]; then
  # curl-pipe install path: fetch the helper too
  TMP_PATCH=$(mktemp)
  curl -fsSL "https://raw.githubusercontent.com/bkrabach/nanoclaw-on-amplifier/main/scripts/apply-onecli-bypass.py" \
    -o "$TMP_PATCH"
  PATCH_SCRIPT="$TMP_PATCH"
fi
say "Applying OneCLI bypass patch for provider=amplifier"
python3 "$PATCH_SCRIPT" "$NANOCLAW_DIR" || warn "OneCLI patch failed — amplifier provider may hit 401 on first spawn"

# Rebuild nanoclaw dist/ so the patched container-runner takes effect on next spawn
if [[ -f "$NANOCLAW_DIR/package.json" ]]; then
  ( cd "$NANOCLAW_DIR" && pnpm run build >/dev/null 2>&1 ) && ok "nanoclaw dist/ rebuilt with patch" || warn "nanoclaw build failed — patch is in src/ but dist/ may be stale; run 'pnpm run build' in $NANOCLAW_DIR"
fi

# DB triple-write (only if data/v2.db exists — which it does after nanoclaw setup)
if [[ -f "$NANOCLAW_DIR/data/v2.db" ]]; then
  sqlite3 "$NANOCLAW_DIR/data/v2.db" <<'SQL'
UPDATE agent_groups       SET agent_provider='amplifier';
UPDATE sessions           SET agent_provider='amplifier';
UPDATE container_configs  SET provider='amplifier';
SQL
  ok "DB columns set to 'amplifier' across agent_groups/sessions/container_configs"
fi

# ---------- Start amplifierd as a service ----------
say "Starting amplifierd"
"$HOME/.nanoclaw-amp/bin/amp-claw" service install || warn "service registration failed; running foreground"
"$HOME/.nanoclaw-amp/bin/amp-claw" restart || true

# ---------- Verify ----------
say "Verifying end-to-end"
"$HOME/.nanoclaw-amp/bin/amp-claw" doctor

cat <<'OUTRO'

────────────────────────────────────────────────────────────────────
✓ nanoclaw-on-amplifier installed.

Your nanoclaw at the configured directory now uses the Amplifier
brain via http://host.docker.internal:8410 (amplifierd in ~/.nanoclaw-amp/).

Switch backends anytime:
  amp-claw backend list
  amp-claw backend set openai
  amp-claw backend set anthropic --model claude-opus-4

Status / logs:
  amp-claw status
  amp-claw logs

Next: continue nanoclaw's channel pairing (`bash nanoclaw.sh` or
read its docs) to add Telegram / Slack / WhatsApp / etc.
────────────────────────────────────────────────────────────────────
OUTRO
