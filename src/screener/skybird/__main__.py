"""`python -m screener.skybird` — the capture container's entry point.

Secrets first, for the reason `screener.boot` loads them first: the chunk length
and the session cap can live in Infisical like anything else, and configuration
read before the fetch would silently be the defaults.
"""

import logging
import sys

from screener.secrets import SecretsError, load_into_environ
from screener.skybird.cli import main

logger = logging.getLogger(__name__)


def _run() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1
    return main()


if __name__ == "__main__":
    sys.exit(_run())
