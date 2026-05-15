#!/usr/bin/env python3
"""Clean up container-runner.ts: remove broken bash-leak from earlier patches
and apply a clean stderr-at-INFO patch."""
import pathlib

p = pathlib.Path("/root/nanoclaw/src/container-runner.ts")
src = p.read_text()

# Remove any broken garbage from earlier patch attempts
broken_lines = [
    "        // AMP_STDERR_TEE: tee container stderr to a file for debugging\n",
    "        try { fs.appendFileSync(/tmp/container-stderr.log, line + \n); } catch {}\n",
    "        try { fs.appendFileSync('/tmp/container-stderr.log', line + '\\n'); } catch {}\n",
]
for b in broken_lines:
    src = src.replace(b, "")

# Ensure the line uses INFO logging so container stderr is visible
src = src.replace(
    "if (line) log.debug(line, { container: agentGroup.folder });",
    "if (line) log.info('[CSTDERR] ' + line, { container: agentGroup.folder });"
)

p.write_text(src)
# Verify the broken garbage is gone
if "appendFileSync" in src and "CSTDERR" not in src:
    print("garbage may remain; check file")
else:
    print("clean")
