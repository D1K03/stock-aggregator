"""Secret loading, from Infisical into the process environment.

The entry point is `load_into_environ()`, called once at startup before
anything reads configuration. Everything downstream keeps reading
`os.environ`, so no module needs to know where its credentials came from.
"""

from screener.secrets.infisical import SecretsError, fetch, load_into_environ

__all__ = ["SecretsError", "fetch", "load_into_environ"]
