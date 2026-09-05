"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/* Recording a question instead of typing it.
 *
 * The transcript goes into the composer rather than straight to the model. That
 * is the whole reason this is worth having: speech recognition mishears proper
 * nouns, and every question here has a ticker in it, so you see "in video"
 * before you pay for an answer about it and can fix it to NVDA. Sending
 * directly would be a worse product and a more expensive one.
 *
 * Format is probed, never assumed. Safari has never supported WebM in
 * MediaRecorder and records AAC in an MP4 container, so a hard-coded
 * audio/webm is a mic button that throws on a Mac. PyAV decodes all three on
 * the other end, which is what it is there for. */

export const FORMATS = [
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

/* Speech at 32 kbps Opus is transparent, and this is what bounds the upload. */
const BITRATE = 32_000;

/* Matches the cap the transcription service enforces. Stopping here rather than
   being refused there means the recording you made is the recording that gets
   used. */
export const MAX_SECONDS = 120;

export type VoiceState = "idle" | "recording" | "working" | "failed";

export function supported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof MediaRecorder !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

function pickFormat(): string | undefined {
  return FORMATS.find((type) => MediaRecorder.isTypeSupported(type));
}

export function useMicrophone(onTranscript: (text: string) => void) {
  const [state, setState] = useState<VoiceState>("idle");
  const [note, setNote] = useState("");
  const [seconds, setSeconds] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const cleanup = useCallback(() => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    // Every track, or the browser leaves the recording indicator lit for the
    // life of the tab and it looks like we are still listening.
    recorder.current?.stream.getTracks().forEach((track) => track.stop());
    recorder.current = null;
  }, []);

  useEffect(() => cleanup, [cleanup]);

  const stop = useCallback(() => {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }, []);

  const start = useCallback(
    async (transcribe: (audio: Blob) => Promise<string>) => {
      if (!supported()) return;
      setNote("");
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        // Denied, or no microphone. Both are the same thing to fix and neither
        // is worth a dialog.
        setState("failed");
        setNote("No microphone, or permission was refused.");
        return;
      }

      const mimeType = pickFormat();
      const media = new MediaRecorder(
        stream,
        mimeType ? { mimeType, audioBitsPerSecond: BITRATE } : undefined
      );
      recorder.current = media;
      const chunks: Blob[] = [];
      media.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };
      media.onstop = async () => {
        cleanup();
        setSeconds(0);
        const audio = new Blob(chunks, { type: media.mimeType });
        if (!audio.size) {
          setState("idle");
          return;
        }
        setState("working");
        try {
          const text = await transcribe(audio);
          setState("idle");
          if (text) onTranscript(text);
          else setNote("Nothing I could make out.");
        } catch (error) {
          setState("failed");
          setNote(error instanceof Error ? error.message : "That failed.");
        }
      };

      media.start();
      setState("recording");
      setSeconds(0);
      timer.current = setInterval(() => {
        setSeconds((elapsed) => {
          // A hard stop rather than a countdown: the service refuses anything
          // longer, and being cut off at the cap beats being refused after it.
          if (elapsed + 1 >= MAX_SECONDS) stop();
          return elapsed + 1;
        });
      }, 1000);
    },
    [cleanup, onTranscript, stop]
  );

  return { state, note, seconds, start, stop };
}
