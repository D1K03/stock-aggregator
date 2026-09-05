"use client";

import { useEffect, useState } from "react";
import { useSteven } from "@/lib/steven";
import { supported, useMicrophone } from "@/lib/voice";

/* Ask by speaking, in either composer.
 *
 * One control shared by Steven's page and the palette, the way the Discord
 * handoff is: two copies of a button with four states is two chances for them
 * to disagree about what "working" looks like.
 *
 * What it produces goes into the box you type in, not to the model. You read
 * it, fix the ticker it misheard, and press enter — so the turn Steven answers
 * is one you approved, and the transcript is visible without anything having to
 * quote it back at you.
 *
 * Hidden entirely where the browser cannot record, rather than shown and
 * broken: there is nothing the reader could do about it. That check runs after
 * mount rather than during render — `MediaRecorder` does not exist on the
 * server, so asking during render would have the server draw nothing and the
 * browser draw a button, which is a hydration mismatch rather than a feature
 * check. */
export default function MicButton({
  onTranscript,
  label = "Ask by voice",
}: {
  onTranscript: (text: string) => void;
  label?: string;
}) {
  const { transcribe } = useSteven();
  const { state, note, seconds, start, stop } = useMicrophone(onTranscript);
  const [ready, setReady] = useState(false);

  useEffect(() => setReady(supported()), []);

  if (!ready) return null;

  const recording = state === "recording";
  const busy = state === "working";

  return (
    <button
      type="button"
      className={`mic ${state}`}
      onClick={() => (recording ? stop() : void start(transcribe))}
      disabled={busy}
      title={note || (recording ? "Stop and transcribe" : label)}
      aria-label={recording ? "Stop recording" : label}
    >
      {recording ? (
        <>
          <span className="mic-dot" aria-hidden="true" />
          <span className="mic-time">{seconds}s</span>
        </>
      ) : busy ? (
        <span className="mic-dot working" aria-hidden="true" />
      ) : (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="9" y="2" width="6" height="11" rx="3" />
          <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 19v3" />
        </svg>
      )}
    </button>
  );
}
