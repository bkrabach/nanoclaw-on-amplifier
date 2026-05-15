/**
 * Amplifier AgentProvider for nanoclaw.
 *
 * Translates nanoclaw's AgentProvider contract into HTTP/SSE calls to a local
 * amplifierd daemon. amplifierd runs on the host in an isolated ~/.nanoclaw-amp/
 * namespace and is reachable from the agent container at host.docker.internal:8410.
 *
 * Contract source: nanoclaw/container/agent-runner/src/providers/types.ts
 *
 * Drops into nanoclaw at: container/agent-runner/src/providers/amplifier.ts
 * Companion host-side container-config: src/providers/amplifier.ts (separate file).
 *
 * MIT licensed; ships with the nanoclaw-on-amplifier wedge skill.
 */

// Self-register with nanoclaw's provider-registry at module load time.
// The barrel `providers/index.ts` imports this file for side-effect.
import { registerProvider } from './provider-registry.js';

// ---------- Types matching nanoclaw's AgentProvider contract ----------
// (intentionally inlined so this file is self-contained; nanoclaw's
// providers/types.ts is the canonical source and these must match.)

export interface ProviderOptions {
  assistantName?: string;
  mcpServers?: Record<string, McpServerConfig>;
  env?: Record<string, string | undefined>;
  additionalDirectories?: string[];
  model?: string;
  effort?: string;
}

export interface McpServerConfig {
  command: string;
  args: string[];
  env: Record<string, string>;
}

export interface QueryInput {
  prompt: string;
  continuation?: string;
  cwd: string;
  systemContext?: { instructions?: string };
}

export type ProviderEvent =
  | { type: "init"; continuation: string }
  | { type: "result"; text: string | null }
  | { type: "error"; message: string; retryable: boolean; classification?: string }
  | { type: "progress"; message: string }
  | { type: "activity" };

export interface AgentQuery {
  push(message: string): void;
  end(): void;
  events: AsyncIterable<ProviderEvent>;
  abort(): void;
}

export interface AgentProvider {
  readonly supportsNativeSlashCommands: boolean;
  query(input: QueryInput): AgentQuery;
  isSessionInvalid(err: unknown): boolean;
}

// ---------- Config ----------
// Read from env (set by host-side amplifier.ts container-config and the
// nanoclaw `-e KEY=VALUE` injection in container-runner.ts).

const AMPLIFIERD_URL = process.env.AMPLIFIERD_URL || "http://host.docker.internal:8410";
const AMPLIFIERD_API_KEY = process.env.AMPLIFIERD_API_KEY || "";
// The `model` knob is how we select an Amplifier backend.  Format:
//   amplifier:<backend>[:<model>]   e.g.  amplifier:anthropic:claude-sonnet-4-5
// We forward the backend to amplifierd via a session-creation override hint;
// the bundle decides which provider module to use based on this.
// (See amp-claw config; the bundle for each backend lives at
//  ~/.nanoclaw-amp/cache/bundles/<backend>.md)
const DEFAULT_BUNDLE = process.env.AMPLIFIER_DEFAULT_BUNDLE || "nanoclaw-amp";
const TURN_TIMEOUT_MS = parseInt(process.env.AMPLIFIER_TURN_TIMEOUT_MS || "600000", 10); // 10 min default
const HEARTBEAT_MS = parseInt(process.env.AMPLIFIER_HEARTBEAT_MS || "5000", 10);

// ---------- Small helpers ----------

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (AMPLIFIERD_API_KEY) h["Authorization"] = `Bearer ${AMPLIFIERD_API_KEY}`;
  return h;
}

async function postJSON<T = unknown>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const r = await fetch(`${AMPLIFIERD_URL}${path}`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new HttpError(r.status, `${r.status} ${r.statusText}: ${text.slice(0, 400)}`);
  }
  return (await r.json()) as T;
}

async function deleteJSON(path: string): Promise<void> {
  await fetch(`${AMPLIFIERD_URL}${path}`, { method: "DELETE", headers: authHeaders() });
}

class HttpError extends Error {
  constructor(public status: number, msg: string) { super(msg); }
}

/** Parses an SSE stream from a Response.body ReadableStream into typed frames. */
async function* parseSSE(response: Response, signal: AbortSignal): AsyncIterable<{
  event?: string;
  data?: string;
  id?: string;
}> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      if (signal.aborted) break;
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE frames separated by \n\n
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const frame: { event?: string; data?: string; id?: string } = {};
        for (const line of raw.split("\n")) {
          if (!line || line.startsWith(":")) continue; // skip keepalive/comments
          const colon = line.indexOf(":");
          if (colon < 0) continue;
          const field = line.slice(0, colon);
          const value = line.slice(colon + 1).replace(/^ /, "");
          if (field === "event") frame.event = value;
          else if (field === "data") frame.data = (frame.data ? frame.data + "\n" : "") + value;
          else if (field === "id") frame.id = value;
        }
        if (frame.event || frame.data) yield frame;
      }
    }
  } finally {
    try { reader.releaseLock(); } catch { /* noop */ }
  }
}

// ---------- amplifierd REST wrappers ----------

interface AmplifierdSession {
  session_id: string;
  status: string;
  bundle_name?: string;
  working_dir?: string;
}

async function createSession(opts: {
  workingDir: string;
  bundleName: string;
  systemAddendum?: string;
  retrySchedule?: number[];
}): Promise<AmplifierdSession> {
  const retries = opts.retrySchedule ?? [0, 1, 2, 4, 8, 16, 32, 60, 120]; // total ≈ 245s
  let lastError: Error | undefined;
  for (let i = 0; i < retries.length; i++) {
    if (retries[i] > 0) await new Promise(r => setTimeout(r, retries[i] * 1000));
    try {
      // amplifierd: POST /sessions creates a session.  config_overrides is
      // declared but unwired in the daemon today; bundle composition is the
      // supported override path.  We rely on the bundle YAML carrying our
      // backend + tool-mcp config + system addendum.
      const body: Record<string, unknown> = {
        bundle_name: opts.bundleName,
        working_dir: opts.workingDir,
      };
      return await postJSON<AmplifierdSession>("/sessions", body);
    } catch (e: any) {
      lastError = e;
      // 503 = prewarm in progress, retry per amplifierd contract
      if (e instanceof HttpError && e.status === 503) continue;
      // Connection refused / network errors are retryable for a short while
      // (daemon may still be starting up after container boot)
      if (e instanceof TypeError && /fetch failed|ECONNREFUSED/i.test(String(e.cause))) continue;
      throw e;
    }
  }
  throw lastError || new Error("createSession exhausted retries");
}

async function deleteSessionSilent(sessionId: string): Promise<void> {
  try { await deleteJSON(`/sessions/${encodeURIComponent(sessionId)}`); } catch { /* noop */ }
}

async function fetchSessionFinalText(sessionId: string, signal: AbortSignal): Promise<string | null> {
  // Fallback path: after a streamed turn completes via execution:end, the
  // canonical `response` text is available via GET /sessions/{id} → stats?
  // No — actually the synchronous /execute returns the response, but we used
  // the stream variant.  We'll instead reassemble from content_block:delta
  // events captured during streaming.  This function exists if we ever need
  // a fallback.
  void sessionId; void signal;
  return null;
}

// ---------- The main provider impl ----------

export class AmplifierProvider implements AgentProvider {
  readonly supportsNativeSlashCommands = false;

  // mcpServers received in constructor describe nanoclaw's MCP server(s).
  // We bridge them into amplifierd via the session's bundle config — but
  // since amplifierd's config_overrides is unwired, we instead rely on the
  // bundle YAML (composed at install time by amp-claw) to declare a tool-mcp
  // entry pointing at `bun run /app/src/mcp-tools/index.ts`.
  constructor(private readonly opts: ProviderOptions) {}

  query(input: QueryInput): AgentQuery {
    return new AmplifierQuery(input, this.opts);
  }

  isSessionInvalid(err: unknown): boolean {
    if (err instanceof HttpError) return err.status === 404;
    const msg = String((err as any)?.message || err || "");
    return /session.*not.*found|thread.*not.*found|invalid.*session|session.*missing/i.test(msg);
  }
}

class AmplifierQuery implements AgentQuery {
  private readonly abortCtrl = new AbortController();
  private readonly pendingFollowups: string[] = [];
  private endedExternally = false;
  private currentSessionId: string | null = null;

  // Async-iterable event channel.  We use a simple queue + pump pattern so
  // the consumer (poll-loop.ts) can `for await (const ev of query.events)`.
  private readonly queue: ProviderEvent[] = [];
  private readonly waiters: Array<(v: IteratorResult<ProviderEvent>) => void> = [];
  private streamFinished = false;

  constructor(
    private readonly input: QueryInput,
    private readonly opts: ProviderOptions,
  ) {
    this.start().catch((err) => {
      this.emit({
        type: "error",
        message: String(err?.message || err),
        retryable: true,
      });
      this.finish();
    });
  }

  // ---- AgentQuery interface ----

  push(message: string): void {
    if (this.endedExternally) return;
    this.pendingFollowups.push(message);
  }

  end(): void {
    this.endedExternally = true;
  }

  abort(): void {
    this.abortCtrl.abort();
    if (this.currentSessionId) {
      // Best-effort cancel (server may already be done)
      postJSON(
        `/sessions/${encodeURIComponent(this.currentSessionId)}/cancel`,
        { immediate: true },
      ).catch(() => undefined);
    }
    this.emit({ type: "error", message: "aborted", retryable: false });
    this.finish();
  }

  get events(): AsyncIterable<ProviderEvent> {
    const self = this;
    return {
      [Symbol.asyncIterator]: () => ({
        next: (): Promise<IteratorResult<ProviderEvent>> => {
          if (self.queue.length > 0) {
            return Promise.resolve({ value: self.queue.shift()!, done: false });
          }
          if (self.streamFinished) {
            return Promise.resolve({ value: undefined as any, done: true });
          }
          return new Promise<IteratorResult<ProviderEvent>>((resolve) => {
            self.waiters.push(resolve);
          });
        },
      }),
    };
  }

  // ---- Internal pump ----

  private emit(ev: ProviderEvent): void {
    const waiter = this.waiters.shift();
    if (waiter) waiter({ value: ev, done: false });
    else this.queue.push(ev);
  }

  private finish(): void {
    this.streamFinished = true;
    while (this.waiters.length > 0) {
      const w = this.waiters.shift()!;
      w({ value: undefined as any, done: true });
    }
  }

  // ---- The lifecycle ----

  private async start(): Promise<void> {
    // 1. Reuse or create amplifierd session.
    //
    //    If we got a `continuation` from a previous turn, it IS our
    //    amplifierd session_id.  amplifierd keeps the in-memory session
    //    alive across turns, so re-creating each turn would be wrong.
    //    We just verify it exists via a HEAD-ish probe; if not, fall through
    //    and create fresh.
    let sessionId = this.input.continuation;
    let isNewSession = false;
    if (sessionId) {
      const exists = await this.sessionExists(sessionId);
      if (!exists) {
        sessionId = undefined;
        // fall through to create
      }
    }
    if (!sessionId) {
      const created = await createSession({
        workingDir: this.input.cwd || "/workspace/agent",
        bundleName: DEFAULT_BUNDLE,
        systemAddendum: this.input.systemContext?.instructions,
      });
      sessionId = created.session_id;
      isNewSession = true;
    }
    this.currentSessionId = sessionId;

    // 2. Yield `init` so the poll-loop can persist our continuation immediately
    //    (this is critical for crash recovery — see add-codex/SKILL.md).
    this.emit({ type: "init", continuation: sessionId });

    // 3. If this is a NEW session and we have a system addendum, inject it
    //    as a "system" message via the context endpoint so amplifierd's
    //    AmplifierSession sees the <message to="..."> contract.
    if (isNewSession && this.input.systemContext?.instructions) {
      await this.injectSystemMessage(sessionId, this.input.systemContext.instructions)
        .catch((err) => {
          // Non-fatal: best effort
          this.emit({ type: "progress", message: `system addendum inject failed: ${err}` });
        });
    }

    // 4. Run the turn(s).  amplifierd allows one execute per session at a
    //    time; mid-turn push() calls queue and we drain between turns.
    const initialPrompt = this.input.prompt;
    await this.runTurn(sessionId, initialPrompt);
    while (this.pendingFollowups.length > 0 && !this.abortCtrl.signal.aborted) {
      const next = this.pendingFollowups.shift()!;
      await this.runTurn(sessionId, next);
    }
    this.finish();
  }

  private async sessionExists(sessionId: string): Promise<boolean> {
    try {
      const r = await fetch(
        `${AMPLIFIERD_URL}/sessions/${encodeURIComponent(sessionId)}`,
        { headers: authHeaders() },
      );
      return r.status === 200;
    } catch {
      return false;
    }
  }

  private async injectSystemMessage(sessionId: string, instructions: string): Promise<void> {
    // amplifierd has POST /sessions/{id}/context/messages.  We inject as
    // role=system with the build-up <message to=...> contract addendum.
    try {
      await postJSON(`/sessions/${encodeURIComponent(sessionId)}/context/messages`, {
        role: "system",
        content: instructions,
      });
    } catch (e: any) {
      // Older amplifierd may use a different endpoint shape.  Don't fail the
      // whole turn — just log progress.
      this.emit({ type: "progress", message: `context inject error: ${e?.message ?? e}` });
    }
  }

  /**
   * Execute one turn via the streaming endpoint.  Streams amplifierd events
   * over SSE, translates them into nanoclaw ProviderEvents, accumulates
   * content_block:delta text into a `result` payload at execution:end.
   */
  private async runTurn(sessionId: string, prompt: string): Promise<void> {
    const turnDeadline = Date.now() + TURN_TIMEOUT_MS;
    const correlationId: string | undefined = undefined; // amplifierd assigns this
    let resultText: string = "";
    let sawExecutionEnd = false;
    let lastActivityAt = Date.now();

    // (1) Kick off the turn (returns 202 with a correlation_id).
    let kickoff: { correlation_id: string };
    try {
      kickoff = await postJSON<{ correlation_id: string }>(
        `/sessions/${encodeURIComponent(sessionId)}/execute/stream`,
        { prompt },
        this.abortCtrl.signal,
      );
    } catch (err: any) {
      this.emit({
        type: "error",
        message: `execute/stream kickoff failed: ${err?.message ?? err}`,
        retryable: err instanceof HttpError ? err.status >= 500 : true,
        classification: err?.status === 429 ? "quota" : undefined,
      });
      return;
    }
    void correlationId; // we filter on session below
    const expectCorrelation = kickoff.correlation_id;

    // (2) Subscribe to events for this session.  amplifierd auto-includes
    //     descendant child-session events, so build-up's sub-agents flow
    //     here too.  We filter to our turn via correlation_id when present.
    const eventsResp = await fetch(
      `${AMPLIFIERD_URL}/events?session=${encodeURIComponent(sessionId)}`,
      { headers: authHeaders(), signal: this.abortCtrl.signal },
    );
    if (!eventsResp.ok) {
      this.emit({
        type: "error",
        message: `events stream returned ${eventsResp.status}`,
        retryable: true,
      });
      return;
    }

    // (3) Heartbeat: every HEARTBEAT_MS, emit `activity` so the poll-loop
    //     touches .heartbeat (host-sweep would kill us otherwise during
    //     long delegations).  We also re-emit on every meaningful event.
    const hb = setInterval(() => {
      if (Date.now() - lastActivityAt > HEARTBEAT_MS / 2) {
        this.emit({ type: "activity" });
        lastActivityAt = Date.now();
      }
    }, HEARTBEAT_MS);

    try {
      for await (const frame of parseSSE(eventsResp, this.abortCtrl.signal)) {
        if (Date.now() > turnDeadline) {
          this.emit({
            type: "error",
            message: `turn exceeded ${TURN_TIMEOUT_MS / 1000}s timeout`,
            retryable: true,
          });
          break;
        }
        if (!frame.data) continue;
        let envelope: any;
        try { envelope = JSON.parse(frame.data); } catch { continue; }
        // amplifierd sse envelope:
        //   { event, data, session_id, timestamp, correlation_id, sequence }
        // Optionally filter by correlation_id of OUR turn so we ignore
        // events from prior turns on the same session.
        if (
          envelope.correlation_id &&
          expectCorrelation &&
          envelope.correlation_id !== expectCorrelation
        ) continue;

        const eventName: string = frame.event || envelope.event || "";
        const data = envelope.data || {};

        // Emit liveness for every event, with light throttling
        if (Date.now() - lastActivityAt > 500) {
          this.emit({ type: "activity" });
          lastActivityAt = Date.now();
        }

        switch (eventName) {
          case "content_block:delta":
            if (typeof data.text === "string") {
              resultText += data.text;
            } else if (typeof data.delta?.text === "string") {
              resultText += data.delta.text;
            }
            break;
          case "tool:pre":
            this.emit({
              type: "progress",
              message: `tool: ${data.tool_name || "?"}`,
            });
            break;
          case "execution:end":
            sawExecutionEnd = true;
            // If amplifierd surfaced a final response string, prefer it.
            if (typeof data.response === "string" && data.response.length > 0) {
              resultText = data.response;
            }
            // Don't break the loop yet — drain a few more frames so
            // late content_block:stop deltas come in.
            break;
          case "approval:required":
            // For v1, we auto-deny tool approvals (rare in build-up
            // because the bundle doesn't include hooks-approval).
            if (envelope.session_id && data.request_id) {
              postJSON(
                `/sessions/${encodeURIComponent(envelope.session_id)}/approvals/${encodeURIComponent(data.request_id)}`,
                { approved: false, message: "auto-denied by amplifier.ts provider (v1)" },
              ).catch(() => undefined);
            }
            break;
          case "session:end":
          case "session:fork":
          case "session:resume":
          case "session:start":
          case "delegate:agent_spawned":
          case "delegate:agent_completed":
            // Informational; activity already emitted above
            break;
          default:
            // Unknown event — pass through as progress for observability
            // (cheap, no big payload dumps).
            break;
        }

        if (sawExecutionEnd) break;
      }
    } finally {
      clearInterval(hb);
    }

    // (4) Emit the final result
    this.emit({ type: "result", text: resultText || null });
  }
}

// ---------- Self-registration at module load ----------
// nanoclaw's provider-registry expects each provider module to call
// registerProvider(name, factory) at top level so the barrel import in
// providers/index.ts triggers registration as a side-effect.
registerProvider("amplifier", (opts: ProviderOptions): AgentProvider => new AmplifierProvider(opts));
