/* `gifenc` ships no types. Declared here rather than pulling a @types package
   that does not exist, and narrowed to the three functions the walk exporter
   actually uses — a blanket `declare module "gifenc"` would type the whole
   library as `any` and hide a signature change behind a green build. */
declare module "gifenc" {
  export type Palette = number[][];

  export interface Encoder {
    writeFrame(
      index: Uint8Array,
      width: number,
      height: number,
      options?: { palette?: Palette; delay?: number; transparent?: boolean }
    ): void;
    finish(): void;
    // Typed over a plain ArrayBuffer so the result is a `BlobPart`;
    // `Uint8Array<ArrayBufferLike>` admits SharedArrayBuffer and is not.
    bytesView(): Uint8Array<ArrayBuffer>;
  }

  export function GIFEncoder(): Encoder;
  export function quantize(data: Uint8ClampedArray | Uint8Array, maxColors: number): Palette;
  export function applyPalette(
    data: Uint8ClampedArray | Uint8Array,
    palette: Palette
  ): Uint8Array;
}
