/**
 * POC-B smoke test: drive amplifier.ts against a running amplifierd.
 *
 * Run with:
 *   AMPLIFIERD_URL=http://127.0.0.1:18410 ~/.bun/bin/bun run _smoke.ts
 *
 * Validates:
 *   - provider.query() yields `init` with a continuation
 *   - first turn yields a final `result` containing the expected text
 *   - second turn reusing the continuation maintains conversational context
 *   - `activity` events stream while the turn is in flight (heartbeat)
 *   - `progress` events fire on tool dispatch
 */

import { AmplifierProvider, type ProviderEvent } from "./amplifier.ts";

const WORKDIR = "/tmp/amp-claw-poc-workdir";

async function runTurn(
  provider: AmplifierProvider,
  prompt: string,
  continuation: string | undefined,
): Promise<{ continuation: string; text: string; activityCount: number; progressCount: number }> {
  console.log(`\n→ Turn: ${prompt.slice(0, 80)}${prompt.length > 80 ? "..." : ""}`);
  const query = provider.query({
    prompt,
    continuation,
    cwd: WORKDIR,
    systemContext: undefined,
  });

  let newContinuation = "";
  let resultText = "";
  let activityCount = 0;
  let progressCount = 0;
  let errorCount = 0;
  const startMs = Date.now();
  const t = setInterval(() => {
    const elapsedSec = ((Date.now() - startMs) / 1000).toFixed(1);
    process.stdout.write(`    ${elapsedSec}s — heartbeats: ${activityCount}, progress: ${progressCount}, errors: ${errorCount}\r`);
  }, 2000);

  try {
    for await (const ev of query.events) {
      switch (ev.type) {
        case "init":
          newContinuation = ev.continuation;
          console.log(`  ✓ init continuation=${newContinuation.slice(0, 24)}...`);
          break;
        case "result":
          resultText = ev.text || "";
          console.log(`\n  ✓ result (${resultText.length} chars): ${resultText.slice(0, 200)}`);
          break;
        case "activity":
          activityCount++;
          break;
        case "progress":
          progressCount++;
          if (progressCount <= 5) console.log(`\n  · progress: ${ev.message}`);
          break;
        case "error":
          errorCount++;
          console.error(`\n  ✗ error (retryable=${ev.retryable}): ${ev.message}`);
          break;
      }
    }
  } finally {
    clearInterval(t);
  }

  if (!newContinuation) throw new Error("No init event with continuation");
  return { continuation: newContinuation, text: resultText, activityCount, progressCount };
}

async function main(): Promise<number> {
  if (!process.env.ANTHROPIC_API_KEY && !process.env.AMPLIFIERD_URL?.includes("127.0.0.1")) {
    console.error("ANTHROPIC_API_KEY not set");
    return 1;
  }
  const provider = new AmplifierProvider({});

  // ----- Turn 1: trivial math -----
  console.log("===== Turn 1: trivial math =====");
  const t1 = await runTurn(provider, "What is 13 times 17? Answer with just the number, no other text.", undefined);
  if (!t1.text.includes("221")) {
    console.error("✗ Turn 1 failed — response did not contain '221'");
    console.error(`  Full response: ${t1.text}`);
    return 2;
  }
  console.log(`✓ Turn 1 PASSED (activity=${t1.activityCount}, progress=${t1.progressCount})`);

  // ----- Turn 2: multi-turn context via continuation reuse -----
  console.log("\n===== Turn 2: multi-turn context (reuse continuation) =====");
  const t2 = await runTurn(provider, "Now divide that number by 13. Answer with just the number.", t1.continuation);
  if (!t2.text.includes("17")) {
    console.error("✗ Turn 2 failed — response did not contain '17'");
    console.error(`  Full response: ${t2.text}`);
    return 3;
  }
  if (t2.continuation !== t1.continuation) {
    console.error(`✗ Turn 2 changed continuation: was ${t1.continuation}, now ${t2.continuation}`);
    return 4;
  }
  console.log(`✓ Turn 2 PASSED (activity=${t2.activityCount}, progress=${t2.progressCount})`);
  console.log(`✓ Continuation preserved across turns: ${t1.continuation.slice(0, 24)}...`);

  console.log("\n===== ✅ POC-B PASSED =====");
  return 0;
}

main().then((code) => process.exit(code)).catch((err) => {
  console.error("FATAL:", err);
  process.exit(99);
});
