"use client";

/* Steven, as a shape.
 *
 * Four soft colour fields inside one clipped circle, each rotating at a
 * different period, blurred so their edges melt together rather than reading as
 * four discs. Because the periods share no common multiple within any sensible
 * viewing time, the composite never visibly repeats: the thing people notice
 * about a cheap loading animation is the loop, so there isn't one.
 *
 * Three states, and the difference between them is only speed and breath:
 *   idle      barely moving, a held breath
 *   thinking  fast, the highlight orbits, the whole orb pulses
 *   speaking  settling back down, one last swell
 *
 * All CSS transforms and opacity, so it runs on the compositor and does not
 * touch layout. Under `prefers-reduced-motion` the rotation stops and it
 * becomes a still gradient, which still reads as an orb.
 */

export type OrbState = "idle" | "thinking" | "speaking";

export default function Orb({
  state = "idle",
  size = 22,
}: {
  state?: OrbState;
  size?: number;
}) {
  return (
    <span
      className={`orb orb-${state}`}
      style={{ width: size, height: size, ["--orb-size" as string]: `${size}px` }}
      role="img"
      aria-label={
        state === "thinking" ? "Steven is thinking" : "Steven"
      }
    >
      <span className="orb-field orb-a" />
      <span className="orb-field orb-b" />
      <span className="orb-field orb-c" />
      <span className="orb-field orb-d" />
      {/* Above the fields and outside the blur, so it stays a crisp glint
          rather than dissolving into them. */}
      <span className="orb-spec" />
    </span>
  );
}
