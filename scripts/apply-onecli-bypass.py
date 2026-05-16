#!/usr/bin/env python3
"""Apply the OneCLI bypass patch to a nanoclaw checkout's container-runner.ts.

Wraps nanoclaw's unconditional OneCLI gateway calls in
`if (provider !== 'amplifier')` so the `amplifier` provider skips the
credential gateway entirely. Other providers (claude, codex, opencode, ollama)
keep the existing flow unchanged.

Idempotent: re-running on an already-patched file is a no-op (detected via
marker string).

Usage:
    python3 apply-onecli-bypass.py <path-to-nanoclaw-checkout>

Or from inside a nanoclaw checkout:
    python3 apply-onecli-bypass.py .

Exit codes:
    0  success (patch applied or already applied)
    1  container-runner.ts not found
    2  OneCLI block not found in expected shape (nanoclaw moved upstream)
"""
import pathlib
import sys


MARKER = "OneCLI gateway skipped (provider=amplifier)"

# The block we replace, EXACTLY as it appears in upstream nanoclaw
# (src/container-runner.ts, inside buildContainerArgs).
NEEDLE = (
    "  if (agentIdentifier) {\n"
    "    await onecli.ensureAgent({ name: agentGroup.name, identifier: agentIdentifier });\n"
    "  }\n"
    "  const onecliApplied = await onecli.applyContainerConfig(args, { addHostMapping: false, agent: agentIdentifier });\n"
    "  if (!onecliApplied) {\n"
    "    throw new Error('OneCLI gateway not applied \u2014 refusing to spawn container without credentials');\n"
    "  }\n"
    "  log.info('OneCLI gateway applied', { containerName });"
)

REPLACEMENT = (
    "  // amp-claw patch: skip OneCLI for `provider=amplifier`. amplifierd\n"
    "  // handles auth on the host; the container reaches it via plain HTTP\n"
    "  // with NO_PROXY bypass injected by our provider's container-config.\n"
    "  if (provider !== 'amplifier') {\n"
    "    if (agentIdentifier) {\n"
    "      await onecli.ensureAgent({ name: agentGroup.name, identifier: agentIdentifier });\n"
    "    }\n"
    "    const onecliApplied = await onecli.applyContainerConfig(args, { addHostMapping: false, agent: agentIdentifier });\n"
    "    if (!onecliApplied) {\n"
    "      throw new Error('OneCLI gateway not applied \u2014 refusing to spawn container without credentials');\n"
    "    }\n"
    "    log.info('OneCLI gateway applied', { containerName });\n"
    "  } else {\n"
    "    log.info('OneCLI gateway skipped (provider=amplifier)', { containerName });\n"
    "  }"
)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: apply-onecli-bypass.py <path-to-nanoclaw-checkout>", file=sys.stderr)
        return 1
    root = pathlib.Path(sys.argv[1])
    target = root / "src" / "container-runner.ts"
    if not target.exists():
        print(f"\u2717 container-runner.ts not found at {target}", file=sys.stderr)
        return 1
    src = target.read_text()
    if MARKER in src:
        print("\u2713 OneCLI bypass already applied (marker found)")
        return 0
    if NEEDLE not in src:
        print(
            "\u26a0 OneCLI block not found in expected shape \u2014 nanoclaw moved upstream.",
            file=sys.stderr,
        )
        print(
            "  The amplifier provider will hit OneCLI 401 on every spawn until this is",
            file=sys.stderr,
        )
        print("  manually patched. See nanoclaw-skill/add-amplifier/SKILL.md for guidance.", file=sys.stderr)
        return 2
    target.write_text(src.replace(NEEDLE, REPLACEMENT))
    print(f"\u2713 OneCLI bypass applied to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
