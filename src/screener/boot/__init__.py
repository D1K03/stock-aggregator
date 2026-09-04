"""Process startup: secrets, schema, then serve.

Run as `python -m screener.boot`, which is the container's `CMD`.

Migrations run here rather than as a separate deploy step, so a container is
authoritative about its own schema and a manual `docker compose up` on the box
is as correct as a pipeline run. The cost is that startup can fail for a schema
reason, which is the right trade: serving against an unknown schema is worse.
"""

from screener.boot.startup import main, prepare_database

__all__ = ["main", "prepare_database"]
