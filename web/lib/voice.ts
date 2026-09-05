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
 * Format is probed, never assumed, and the probe is not a formality. Safari
 * recorded MP4 with AAC and nothing else from 14.1 until 18.4, so a hard-coded
 * audio/webm is a mic button that throws on any Mac more than a year old — and
 * `audio/mp4` is listed bare because Safari answered false to it whenever a
 * codecs parameter was attached. PyAV decodes all of them on the other end,
 * which is what it is there for: the transcriber has already been proven
 * against an AAC-in-MP4 file, which is exactly what older Safari produces.
 *
 * Nothing here assumes the probe was honest, either. Safari has historically
 * accepted a type from `isTypeSupported` and then thrown on the constructor
 * anyway, so construction is attempted three ways and the microphone is
 * released if all of them fail. A stream left open is a recording indicator
 * that stays lit for the life of the tab, which reads as still listening. */

export const FORMATS = [
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  // Bare, with no codecs parameter: Safari answers false to the qualified form.
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

export function pickFormat(): string | undefined {
  // Present since Safari 14.1, but guarded rather than assumed: without it the
  // probe throws and the button dies before it has asked for anything.
  if (typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return FORMATS.find((type) => MediaRecorder.isTypeSupported(type));
}

/* Build a recorder, giving way on each refusal rather than failing on the
   first. The preferred form carries a bitrate; then the type alone, because
   Safari has rejected options objects it did not recognise; then nothing at
   all, which is the browser's own default and always works if anything does. */
function makeRecorder(stream: MediaStream): MediaRecorder | null {
  const mimeType = pickFormat();
  const attempts: (MediaRecorderOptions | undefined)[] = mimeType
    ? [{ mimeType, audioBitsPerSecond: BITRATE }, { mimeType }, undefined]
    : [undefined];
  for (const options of attempts) {
    try {
      return new MediaRecorder(stream, options);
    } catch {
      // Next shape.
    }
  }
  return null;
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

      const media = makeRecorder(stream);
      if (!media) {
        // Release the microphone by hand: `cleanup` reaches the tracks through
        // the recorder, and there is no recorder to reach them through.
        stream.getTracks().forEach((track) => track.stop());
        setState("failed");
        setNote("This browser will not record audio.");
        return;
      }
      recorder.current = media;
      const chunks: Blob[] = [];
      media.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };
      media.onstop = async () => {
        cleanup();
        setSeconds(0);
        // Safari can report an empty `mimeType` here even having recorded
        // happily. The server sniffs the container rather than trusting the
        // header, so this only has to be plausible, not authoritative.
        const audio = new Blob(chunks, { type: media.mimeType || "audio/mp4" });
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

      try {
        media.start();
      } catch {
        stream.getTracks().forEach((track) => track.stop());
        recorder.current = null;
        setState("failed");
        setNote("This browser would not start recording.");
        return;
      }
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
