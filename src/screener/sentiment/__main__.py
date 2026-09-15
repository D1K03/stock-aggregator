"""`python -m screener.sentiment` — the sentiment container's entry point.

No secrets are loaded, on the same terms as `screener.transcribe`. This service
holds no credentials, opens no database connection and calls nothing outside the
compose network: the only thing it needs is its weights, and those are in the
image.
"""

import logging

from screener.sentiment.server import serve


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
