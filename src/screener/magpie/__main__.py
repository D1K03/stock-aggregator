"""python -m screener.magpie — the scraper container's command."""

import logging
import sys

from screener.secrets import SecretsError, load_into_environ


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        load_into_environ()
    except SecretsError as exc:
        logging.error("could not load secrets: %s", exc)
        return 1

    from screener.magpie.server import serve

    return serve()


if __name__ == "__main__":
    sys.exit(main())
