import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits .next/standalone: a self-contained server plus only the node_modules
  // it actually reaches. The runtime image then carries no build toolchain and
  // no dev dependencies.
  output: "standalone",

  // resvg is a native addon: a .node binary, which the bundler cannot inline
  // and refuses outright ("non-ecmascript placeable asset"). Left external it
  // is required at runtime from node_modules, which is what a native module
  // needs anyway.
  serverExternalPackages: ["@resvg/resvg-js"],

  // Traced by what is imported, so a file only ever opened with `fs` at
  // runtime is left out. The chart rasteriser reads the Geist faces that way,
  // and without this the deployed container would quietly draw Discord's
  // charts in whatever font resvg fell back to.
  outputFileTracingIncludes: {
    "/api/render": ["./node_modules/geist/dist/fonts/geist-sans/*.ttf"],
  },
};

export default nextConfig;
