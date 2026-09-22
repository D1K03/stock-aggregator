"""Secret loading, from Infisical into the process environment.

The entry point is `load_into_environ()`, called once at startup before
anything reads configuration, and then `watch()` in every long-running process,
which keeps `os.environ` in step with Infisical from then on. Everything
downstream keeps reading `os.environ`, so no module needs to know where its
credentials came from, or when they last changed.
"""

from screener.secrets.infisical import (
    SecretsError,
    fetch,
    load_into_environ,
    refresh,
    watch,
)

__all__ = ["SecretsError", "fetch", "load_into_environ", "refresh", "watch"]
