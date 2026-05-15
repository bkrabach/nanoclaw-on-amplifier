# nanoclaw-on-amplifier

Wrap [nanoclaw](https://github.com/nanocoai/nanoclaw) with an Amplifier-powered provider that replaces Claude Code as the agent brain. The full Amplifier ecosystem (Anthropic, OpenAI, Gemini, Azure, ChatGPT-subscription, Copilot, Ollama, vLLM, chat-completions, mock) lives behind a single `amplifier` provider that nanoclaw sees.

## What this gives you

- **Drop-in upgrade for nanoclaw** — runs nanoclaw's own installer first, then wedges our provider in via nanoclaw's existing skill mechanism. `git pull` in your nanoclaw checkout is safe.
- **One provider, every backend** — `amp-claw backend set anthropic|openai|gemini|copilot|ollama|...` switches your agent's LLM without touching nanoclaw.
- **Build-up bundle by default** — delegation-first orchestration: parent thread stays clean, work fans out to specialist sub-agents.
- **All of nanoclaw's MCP tools, free** — `send_message`, `schedule_task`, `ask_user_question`, agent-to-agent routing, and 12 more. Wired via Amplifier's `tool-mcp` bridge to nanoclaw's stdio MCP server.
- **Isolated namespace** — everything we install lives in `~/.nanoclaw-amp/`. If you already use the Amplifier CLI (`~/.amplifier/`), we don't touch it.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/bkrabach/nanoclaw-on-amplifier/main/install.sh | bash
```

The installer:
1. Bootstraps any missing system deps (uv, Docker — delegates to nanoclaw where it can).
2. Runs nanoclaw's own installer (latest, unmodified).
3. Installs amplifierd into `~/.nanoclaw-amp/` (isolated from any other Amplifier install).
4. Wedges the `amplifier` provider into your nanoclaw checkout via the `/add-amplifier` skill.
5. Asks which Amplifier backend you want (Anthropic / OpenAI / Gemini / Ollama / ...) and one API key.
6. Hands off to nanoclaw's standard channel-pairing wizard (full menu — Telegram, Slack, Discord, WhatsApp, ...).

## Status

Pre-alpha. See [SCRATCH.md](https://github.com/bkrabach/nanoclaw-on-amplifier/blob/main/SCRATCH.md) for the working design notes.
