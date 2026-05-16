# /add-amplifier — wire the Amplifier brain into nanoclaw

Run this Claude Code skill inside a nanoclaw checkout. It drops a single new
provider (`amplifier`) that proxies all agent calls to an `amplifierd` daemon
running on the host in `~/.nanoclaw-amp/`. The user then picks **which**
Amplifier backend (anthropic / openai / openai-chatgpt / azure / gemini /
copilot / chat-completions / ollama / vllm / mock) via `amp-claw backend set`
— all without ever touching nanoclaw trunk again.

## Why this skill exists

Nanoclaw's `AgentProvider` interface (`container/agent-runner/src/providers/types.ts`)
is a stable plug-in point. The `/add-codex`, `/add-opencode`, and
`/add-ollama-provider` skills already use it. This skill follows the same
pattern, but the wired backend is **Amplifier**, which itself fans out to any
of the `amplifier-module-provider-*` modules. One nanoclaw provider, all
Amplifier backends.

The user keeps a stock, `git pull`-able nanoclaw. Our wedge is a few files
under `src/providers/`, `container/agent-runner/src/providers/`, and a
two-line append to each of the two `index.ts` barrels.

## What it does

1. Verifies the nanoclaw checkout is v2 (`pnpm-workspace.yaml` exists, `src/db/schema.ts` references `v2.db`).
2. Drops:
   - `src/providers/amplifier.ts` — host-side container-config (returns the env block to inject into the agent container).
   - `container/agent-runner/src/providers/amplifier.ts` — the `AgentProvider` impl that talks HTTP/SSE to amplifierd.
3. Appends `import './amplifier.js';` to:
   - `src/providers/index.ts`
   - `container/agent-runner/src/providers/index.ts`
4. (One-time only) Installs `amplifierd` into `~/.nanoclaw-amp/` (isolated venv; never touches `~/.amplifier/` or any global tool dir).
5. Writes `~/.nanoclaw-amp/config.yaml` with the user's chosen backend + bundle + port.
6. Updates the user's default agent group(s) to use `provider=amplifier` via the DB triple-write (`agent_groups.agent_provider`, `sessions.agent_provider`, `container_configs.provider`).
7. Starts amplifierd (launchd on macOS, systemd --user on Linux, manual on WSL).
8. Verifies end-to-end by spawning a quick test session that hits the chosen Amplifier backend.

Idempotent: re-running the skill overwrites the dropped files, re-appends barrel imports if missing, leaves the backend config alone unless `--reconfigure` is passed.

---

## Prerequisites

- A working nanoclaw v2 checkout (run `bash nanoclaw.sh` first if you haven't).
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- An API key for at least one Amplifier backend you want to use.

Claude Code is NOT required at runtime. It's only invoked here as the skill
runner. If you don't have it, run `bash install.sh` directly from
[bkrabach/nanoclaw-on-amplifier](https://github.com/bkrabach/nanoclaw-on-amplifier)
which does the same work without the Claude Code wrapper.

---

## Step 1 — Sanity check the nanoclaw checkout

```bash
test -f "$PWD/pnpm-workspace.yaml" || { echo "Not in a nanoclaw checkout"; exit 1; }
test -f "$PWD/src/db/schema.ts" || { echo "Not a v2 schema"; exit 1; }
test -d "$PWD/container/agent-runner/src/providers" || { echo "Agent-runner providers dir missing"; exit 1; }
echo "✓ nanoclaw v2 detected"
```

## Step 2 — Drop the host-side provider container-config

Write `src/providers/amplifier.ts`:

```ts
// src/providers/amplifier.ts — Amplifier provider host-side container config.
// Returns env vars the agent container needs to reach amplifierd on the host.
// Pattern mirrors src/providers/codex.ts (see /add-codex skill).

import { registerProvider, type ProviderContainerConfigFn } from './provider-container-registry.js';

const configFn: ProviderContainerConfigFn = ({ agentGroup, container }) => {
  // amplifierd port is configurable via ~/.nanoclaw-amp/config.yaml
  // (read by amp-claw at start; falls through to 8410 if unset).
  const port = process.env.AMPLIFIERD_PORT || '8410';
  return {
    env: {
      AMPLIFIERD_URL: `http://host.docker.internal:${port}`,
      AMPLIFIER_DEFAULT_BUNDLE: 'nanoclaw-amp',
      NO_PROXY: 'host.docker.internal,localhost,127.0.0.1',
      no_proxy: 'host.docker.internal,localhost,127.0.0.1',
    },
  };
};

registerProvider('amplifier', configFn);
```

Then append the barrel import:

```bash
LINE="import './amplifier.js';"
grep -qxF "$LINE" src/providers/index.ts || echo "$LINE" >> src/providers/index.ts
```

## Step 3 — Drop the container-side AgentProvider impl

Copy from this repo (or curl it):

```bash
cp "$NANOCLAW_AMP_SKILL_DIR/amplifier.ts" container/agent-runner/src/providers/amplifier.ts
# or:
curl -fsSL https://raw.githubusercontent.com/bkrabach/nanoclaw-on-amplifier/main/nanoclaw-provider/amplifier.ts \
  -o container/agent-runner/src/providers/amplifier.ts
```

Append the agent-runner barrel:

```bash
LINE="import './amplifier.js';"
grep -qxF "$LINE" container/agent-runner/src/providers/index.ts || \
  echo "$LINE" >> container/agent-runner/src/providers/index.ts
```

The `/app/src` path is a bind mount of `container/agent-runner/src` (see
`container/src/container-runner.ts:313`), so the new file is picked up on the
next container spawn — no rebuild, no restart needed. (Recon confirmed this:
`add-opencode/SKILL.md:96-104` describes a v2-sessions overlay that no longer
applies in current main.)

## Step 3b — Patch container-runner.ts to skip OneCLI for `provider=amplifier`

Nanoclaw's `src/container-runner.ts` unconditionally calls the OneCLI gateway
on every container spawn (`onecli.ensureAgent` + `onecli.applyContainerConfig`)
and throws if either fails. OneCLI is a credential-proxy service at
`app.onecli.sh` that requires a real account. The `amplifier` provider does
not use it — amplifierd runs on the host and the container reaches it via
plain HTTP using the `NO_PROXY` entries our provider injects.

Without this patch, a fresh user with no OneCLI account hits a hard 401 on
every message. With it, OneCLI is bypassed for `provider=amplifier` only;
other providers (claude, codex, opencode, ollama) keep the unchanged flow.

Apply the patch (idempotent — re-running is a no-op):

```bash
python3 - <<'PY'
import pathlib
p = pathlib.Path("src/container-runner.ts")
src = p.read_text()
marker = "OneCLI gateway skipped (provider=amplifier)"
if marker in src:
    print("✓ OneCLI bypass already applied")
    raise SystemExit
needle = """  if (agentIdentifier) {
    await onecli.ensureAgent({ name: agentGroup.name, identifier: agentIdentifier });
  }
  const onecliApplied = await onecli.applyContainerConfig(args, { addHostMapping: false, agent: agentIdentifier });
  if (!onecliApplied) {
    throw new Error('OneCLI gateway not applied — refusing to spawn container without credentials');
  }
  log.info('OneCLI gateway applied', { containerName });"""
if needle not in src:
    raise SystemExit("⚠ OneCLI block not found in expected shape — nanoclaw moved upstream; apply manually")
replacement = """  // amp-claw patch: skip OneCLI for `provider=amplifier`. amplifierd
  // handles auth on the host; the container reaches it via plain HTTP
  // with NO_PROXY bypass injected by our provider's container-config.
  if (provider !== 'amplifier') {
    if (agentIdentifier) {
      await onecli.ensureAgent({ name: agentGroup.name, identifier: agentIdentifier });
    }
    const onecliApplied = await onecli.applyContainerConfig(args, { addHostMapping: false, agent: agentIdentifier });
    if (!onecliApplied) {
      throw new Error('OneCLI gateway not applied — refusing to spawn container without credentials');
    }
    log.info('OneCLI gateway applied', { containerName });
  } else {
    log.info('OneCLI gateway skipped (provider=amplifier)', { containerName });
  }"""
p.write_text(src.replace(needle, replacement))
print("✓ OneCLI bypass applied")
PY
pnpm run build  # rebuild dist/ so the patched container-runner takes effect on next spawn
```

This is the *one* unavoidable trunk patch this skill applies — every other
piece of the wedge lives in our own files. Upstream nanoclaw doesn't yet
expose a per-provider OneCLI opt-out hook, so we patch the conditional in
directly. If nanoclaw refactors this block, the `python3` script above will
print a warning and skip cleanly; rerun the skill after updating the needle.

## Step 4 — Install amplifierd in an isolated namespace

Skip if `~/.nanoclaw-amp/bin/amplifierd` already exists.

```bash
mkdir -p ~/.nanoclaw-amp/{bin,data,data/cache,data/sessions,logs}
uv venv --python 3.12 ~/.nanoclaw-amp/venv
VIRTUAL_ENV=~/.nanoclaw-amp/venv uv pip install "git+https://github.com/microsoft/amplifierd@main"
cat > ~/.nanoclaw-amp/bin/amplifierd <<'EOF'
#!/usr/bin/env bash
exec ~/.nanoclaw-amp/venv/bin/amplifierd "$@"
EOF
chmod +x ~/.nanoclaw-amp/bin/amplifierd
```

This pulls in `amplifier-core` and `amplifier-foundation` as transitive
dependencies, plus FastAPI/uvicorn for the daemon. The whole footprint lives
under `~/.nanoclaw-amp/` — your existing `~/.amplifier/` (CLI install) is
untouched.

## Step 5 — Pick a backend and write the bundle

Ask the user which Amplifier backend to use. Default is `anthropic`.

```text
Which Amplifier backend?
  1) anthropic         (claude-sonnet-4-5; recommended; most tested)
  2) openai            (gpt-4 / gpt-5)
  3) openai-chatgpt    (ChatGPT subscription via Codex OAuth — no API billing)
  4) azure-openai
  5) gemini            (1M context)
  6) chat-completions  (llama.cpp / vLLM / LM Studio / LocalAI)
  7) ollama
  8) vllm
  9) copilot
  10) mock             (tests, no real LLM)

[1]: _
```

Then ask for the API key for that backend (paste once; stored at
`~/.nanoclaw-amp/keys.env`). For OAuth-based backends (chatgpt, copilot),
launch the OAuth flow instead.

Write `~/.nanoclaw-amp/config.yaml`:

```yaml
backend: anthropic                   # whichever was picked
model: claude-sonnet-4-5             # backend-specific default
bundle: build-up                     # the Amplifier bundle to use as the brain
port: 8410
home_dir: ~/.nanoclaw-amp/data       # isolated from ~/.amplifierd or ~/.amplifier
projects_dir: ~/.nanoclaw-amp/data/projects
```

Write `~/.nanoclaw-amp/bundles/nanoclaw-amp.md` (composed from build-up + the
chosen provider + tool-mcp bridge to nanoclaw):

```yaml
---
bundle:
  name: nanoclaw-amp
  version: 0.1.0
  description: build-up + chosen provider + MCP bridge to nanoclaw's stdio MCP server

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=experiments/build-up/build-up-foundation.md

providers:
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@main
    config:
      default_model: claude-sonnet-4-5

tools:
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
    config:
      servers:
        nanoclaw:
          command: bun
          args: ["run", "/app/src/mcp-tools/index.ts"]
          transport: stdio
          env: {}
---

# nanoclaw output contract addendum
#
# When this agent is running inside a nanoclaw container, *every* outbound
# user-facing message must be wrapped in `<message to="name">...</message>`
# blocks.  Unwrapped text is treated as internal scratchpad and is NOT
# delivered to any channel.  Names are resolved from the `destinations`
# table at session start.  See container/agent-runner/src/destinations.ts.
```

(The skill writes this composed bundle file.  Switching backends via
`amp-claw backend set <name>` rewrites this file with a different provider
module and restarts amplifierd.)

## Step 6 — Triple-write the DB columns

```bash
sqlite3 data/v2.db <<'SQL'
UPDATE agent_groups       SET agent_provider='amplifier';
UPDATE sessions           SET agent_provider='amplifier';
UPDATE container_configs  SET provider='amplifier';
SQL
```

Recon confirmed this is the safe pattern (`add-codex/SKILL.md:108`): the
host-side resolver falls through `sessions > agent_groups > container.json`,
and the in-container runner reads `container.json` (materialized from DB on
each spawn).  Setting all three covers every dispatch path.

## Step 7 — Start amplifierd

Platform-specific service registration. On Linux with systemd user services:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/nanoclaw-amp.service <<EOF
[Unit]
Description=amplifierd for nanoclaw

[Service]
ExecStart=$HOME/.nanoclaw-amp/bin/amplifierd serve --port 8410 \\
  --bundle nanoclaw-amp=file://$HOME/.nanoclaw-amp/bundles/nanoclaw-amp.md \\
  --default-bundle nanoclaw-amp
Restart=on-failure
EnvironmentFile=$HOME/.nanoclaw-amp/keys.env

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now nanoclaw-amp.service
```

macOS uses launchd (see `install.sh`).

## Step 8 — Verify

```bash
amp-claw doctor
```

Expected output:

```
✓ amplifierd reachable at http://127.0.0.1:8410
✓ Backend: anthropic / claude-sonnet-4-5
✓ Bundle nanoclaw-amp loaded
✓ MCP bridge to nanoclaw configured
✓ Default group provider = amplifier
✓ Round-trip test: 13 × 17 = 221 (took 4.2s)
```

The round-trip test exercises POST /sessions → POST /execute → response, in
the same path nanoclaw will use at runtime.

---

## Known limitations

- amplifierd's `config_overrides` field on POST /sessions is declared in the
  model but **not wired** as of v0.1.0. We compose the bundle YAML once at
  install/reconfigure time and let amplifierd read it. Switching backends
  rewrites the YAML and restarts the daemon.
- OneCLI's HTTPS proxy is unconditionally injected into the container
  (`container-runner.ts:389` refuses to spawn without it). Our
  `NO_PROXY=host.docker.internal` bypass works the same way
  `/add-ollama-provider` does it.
- If the user `git pull`s nanoclaw and the upstream `AgentProvider` interface
  changes, we need to update `amplifier.ts` to match. We track that
  interface as our only upstream contract.
- We do **not** modify `container/Dockerfile` or `container/agent-runner/package.json`.
  Our provider uses Bun's built-in `fetch()` (no client SDK needed),
  so no image rebuild.
