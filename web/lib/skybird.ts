/* The shape the status service returns from /api/skybird.
 *
 * Everything here is real, unlike the concept data behind the Overview: these
 * rows are captures that happened and words somebody said. */

export type SkybirdSession = {
  id: number;
  platform: string;
  external_id: string;
  channel: string | null;
  title: string | null;
  source_url: string;
  /** Built server-side, because Twitch needs a `parent` matching this host and
   *  that is configuration the web container deliberately does not hold. Null
   *  until a probe names the broadcast, which is what a YouTube handle URL is. */
  embed_url: string | null;
  state:
    | "requested"
    | "starting"
    | "running"
    | "paused"
    | "stopping"
    | "stopped"
    | "failed";
  stop_reason: string | null;
  requested_by: string;
  requested_at: string;
  started_at: string | null;
  stopped_at: string | null;
  chunk_seconds: number;
  chunks_ok: number;
  chunks_failed: number;
  chunks_dropped: number;
  last_error: string | null;
  /** Seconds of audio captured, across every reconnect and every pause. This is
   *  what the transcript's offsets count, so it survives a pause and a restart. */
  captured_seconds: number;
  segment_count: number;
  last_segment_at: string | null;
  live: boolean;
};

export type SkybirdSegment = {
  seq: number;
  chunk_seq: number;
  captured_at: string;
  offset_seconds: number;
  duration_seconds: number;
  text: string;
};

export type SkybirdListing = {
  sessions: SkybirdSession[];
  /** Named by the server so this file does not hold a second copy of the
   *  registry, which would go stale the day an adapter is added. */
  platforms: { code: string; display_name: string }[];
  max_sessions: number;
  chunk_seconds: number;
};

export type SkybirdTranscript = {
  session: SkybirdSession;
  segments: SkybirdSegment[];
};

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    // Same origin behind Caddy, but explicit: the session cookie is the whole
    // authorisation and a default that omitted it would 401 confusingly.
    credentials: "include",
    cache: "no-store",
    ...init,
  });
  if (response.status === 401) throw new Error("unauthorised");
  if (!response.ok) {
    // The server's own sentence where there is one — "that is not a stream I
    // can capture", "stop one first" — because those are the errors a person
    // can actually do something about.
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error ?? `skybird request failed: ${response.status}`);
  }
  return response.json();
}

function post<T>(path: string, body: unknown): Promise<T> {
  return call<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchSessions(): Promise<SkybirdListing> {
  return call<SkybirdListing>("/api/skybird");
}

export function fetchTranscript(
  session: number,
  after: number
): Promise<SkybirdTranscript> {
  const query = new URLSearchParams({ session: String(session), after: String(after) });
  return call<SkybirdTranscript>(`/api/skybird/transcript?${query}`);
}

export function startCapture(url: string): Promise<{ session: SkybirdSession }> {
  return post("/api/skybird/start", { url });
}

export function stopCapture(id: number): Promise<{ session: SkybirdSession }> {
  return post("/api/skybird/stop", { id });
}

/* Pause holds the stream without giving it up: no ffmpeg and no share of the
   transcriber, but nobody else can capture it and the transcript carries on
   from where it stopped when you resume. */
export function pauseCapture(id: number): Promise<{ session: SkybirdSession }> {
  return post("/api/skybird/pause", { id });
}

export function resumeCapture(id: number): Promise<{ session: SkybirdSession }> {
  return post("/api/skybird/resume", { id });
}

export function deleteCapture(id: number): Promise<{ deleted: number }> {
  return post("/api/skybird/delete", { id });
}

/* Seconds from the start of the capture, as a clock. Streams run for hours, so
   the hour is dropped only when there isn't one. */
export function stamp(offset: number): string {
  const total = Math.max(0, Math.floor(offset));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

/* What the state means, in words. `stopped` and `failed` look the same in a
   list of finished captures unless the reason is spelled out. */
export function describe(session: SkybirdSession): string {
  switch (session.state) {
    case "requested":
      return "waiting for the capture container";
    case "starting":
      return "connecting to the stream";
    case "running":
      return `capturing · ${session.chunk_seconds}s chunks`;
    case "paused":
      return `paused · ${stamp(session.captured_seconds)} captured`;
    case "stopping":
      return "stopping";
    case "failed":
      return session.stop_reason ?? "failed";
    default:
      return session.stop_reason === "stream_ended" ? "the stream ended" : "stopped";
  }
}
