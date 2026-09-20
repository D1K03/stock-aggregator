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

  /* One origin in development, the way Caddy gives us one in production.
   *
   * Deployed, `deploy/caddy/Caddyfile` fronts the status service and this app
   * on a single port, so the browser's `/api/...` is same-origin and the
   * session cookie just works. Locally there is no Caddy: the status service is
   * a separate process on 8080, and a page on 3000 calling it cross-origin is
   * refused, because `/api/*` sends no CORS headers — correctly, since in
   * production nothing is ever cross-origin.
   *
   * Rewrites rather than a proxy in front of both. A proxy is the obvious fix
   * and it is the wrong one here: buffering Next's dev responses through one
   * breaks hydration, so every page renders and nothing on it responds to a
   * click, which is a far worse failure than the CORS error it replaced.
   *
   * Gated on the dev server so a production build cannot pick up a rewrite to
   * localhost. `next build` never evaluates this branch.
   */
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    const api = process.env.LOCAL_API_ORIGIN ?? "http://localhost:8080";
    // The handles from the Caddyfile, and nothing beyond them: anything not
    // named here is this app's own route, including /api/render.
    return [
      { source: "/api/ask", destination: `${api}/api/ask` },
      { source: "/api/model", destination: `${api}/api/model` },
      { source: "/api/models", destination: `${api}/api/models` },
      { source: "/api/handoff", destination: `${api}/api/handoff` },
      { source: "/api/audit", destination: `${api}/api/audit` },
      { source: "/api/screen/:path*", destination: `${api}/api/screen/:path*` },
      { source: "/api/screen", destination: `${api}/api/screen` },
      { source: "/api/playground/:path*", destination: `${api}/api/playground/:path*` },
      { source: "/api/playground", destination: `${api}/api/playground` },
      { source: "/api/magpie/:path*", destination: `${api}/api/magpie/:path*` },
      { source: "/api/magpie", destination: `${api}/api/magpie` },
      { source: "/api/skybird/:path*", destination: `${api}/api/skybird/:path*` },
      { source: "/api/skybird", destination: `${api}/api/skybird` },
      { source: "/api/transcribe", destination: `${api}/api/transcribe` },
      { source: "/auth/:path*", destination: `${api}/auth/:path*` },
      { source: "/status", destination: `${api}/status` },
      { source: "/health", destination: `${api}/health` },
      { source: "/ready", destination: `${api}/ready` },
    ];
  },
};

export default nextConfig;
