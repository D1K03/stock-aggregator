"""A very small status service — the Cloudflare tunnel's origin.

Three read-only JSON endpoints:

    /health   liveness. Touches nothing. What the container healthcheck hits.
    /ready    can this process reach its database right now.
    /status   which build is running, and for how long.

Deliberately stdlib rather than FastAPI. Three GETs with no request bodies, no
validation and no auth — Cloudflare Access owns that — do not justify starlette,
pydantic and an async runtime in a repo whose defining property is that it has
almost no dependencies. When the deferred web UI arrives it will want a real
framework, and replacing this one module then changes nothing around it: not
the tunnel, not the compose unit, not the deploy smoke test.
"""

from screener.health.server import DEFAULT_PORT, build_server, serve

__all__ = ["DEFAULT_PORT", "build_server", "serve"]
