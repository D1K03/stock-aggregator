"use client";

import { useSteven } from "@/lib/steven";

/* "Continue in Discord", wherever the conversation is.
 *
 * One button used by both surfaces rather than one per surface. The state it
 * reads and the label it shows are the same object in `StevenProvider`, so
 * handing off from the palette and handing off from Steven's own page are the
 * same act reported the same way, and neither can drift into telling you
 * something the other would not.
 *
 * It says what happened rather than only that it was clicked: sending, sent, or
 * the reason it failed in the tooltip. A handoff that silently did nothing would
 * leave you waiting on Discord for a message that is never coming. */
export default function DiscordHandoff() {
  const { handoffState, handoffNote, handoff } = useSteven();

  return (
    <button
      className={`chip chip-discord ${handoffState}`}
      onClick={handoff}
      disabled={handoffState === "sending"}
      title={handoffNote || "Steven will message you on Discord"}
    >
      <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
        <path d="M13.5 3.2A12 12 0 0 0 10.6 2.3l-.2.4a11 11 0 0 1 2.6 1 12.6 12.6 0 0 0-7.9 0 11 11 0 0 1 2.6-1l-.2-.4a12 12 0 0 0-3 .9C1.9 6.3 1.4 9.3 1.7 12.2a12.2 12.2 0 0 0 3.7 1.9l.8-1.1a8 8 0 0 1-1.2-.6l.3-.2a8.7 8.7 0 0 0 7.4 0l.3.2a8 8 0 0 1-1.2.6l.8 1.1a12.2 12.2 0 0 0 3.7-1.9c.4-3.4-.5-6.4-2.8-9zM6.2 10.4c-.7 0-1.3-.7-1.3-1.5s.6-1.5 1.3-1.5 1.3.7 1.3 1.5-.6 1.5-1.3 1.5zm3.6 0c-.7 0-1.3-.7-1.3-1.5s.6-1.5 1.3-1.5 1.3.7 1.3 1.5-.6 1.5-1.3 1.5z" />
      </svg>
      {handoffState === "sending"
        ? "Messaging…"
        : handoffState === "sent"
        ? "Sent to Discord"
        : handoffState === "failed"
        ? "Could not send"
        : "Continue in Discord"}
    </button>
  );
}
