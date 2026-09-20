import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* Without this `next dev` refuses to serve /_next/* to 127.0.0.1 and the
     dashboard is a blank white page. Not an obvious blank either: every element
     on it is a framer-motion node server-rendered at `opacity: 0` and animated
     to 1 on mount, so when the dev bundle is blocked the markup is all there,
     the page returns 200, and nothing is visible. Naming both spellings because
     "localhost" and "127.0.0.1" are different origins to a browser and people
     type both. Development only -- Next ignores it in a production build. */
  allowedDevOrigins: ["127.0.0.1", "localhost"],

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

  /* One origin for `npm run dev`, which is the arrangement everything else
     assumes and the only one a session survives.
   *
   * In production Caddy owns this: /api/*, /auth/*, /status, /health and /ready
   * go to the api container and everything else falls through to Next. There is
   * no Caddy in front of `next dev`, so without this the dashboard runs on one
   * port and the status service on another — and then the session cookie is
   * sent (cookies ignore port) but the browser blocks *reading* the response,
   * because the status service answers no Access-Control-Allow-Origin. The page
   * renders and every panel is empty, which looks like a broken page rather
   * than a missing proxy.
   *
   * `afterFiles`, so Next's own routes are matched first and /api/render — the
   * chart rasteriser, which really is a Next route — is not proxied away.
   *
   * Development only, checked here rather than trusted to configuration: a
   * rewrite left on in production would be a second, unauthenticated path to
   * the api container that no Caddyfile mentions. */
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    const api = process.env.LOCAL_API_ORIGIN ?? "http://127.0.0.1:8080";
    return {
      beforeFiles: [],
      afterFiles: [
        { source: "/api/:path*", destination: `${api}/api/:path*` },
        { source: "/auth/:path*", destination: `${api}/auth/:path*` },
        { source: "/status", destination: `${api}/status` },
        { source: "/health", destination: `${api}/health` },
        { source: "/ready", destination: `${api}/ready` },
      ],
      fallback: [],
    };
  },
};

export default nextConfig;
