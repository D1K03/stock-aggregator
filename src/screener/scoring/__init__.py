"""Daily bars into percentiles, a pillar score and a dated snapshot.

Five of the seven modules are pure -- they take plain values and return plain
values -- as `screener.ingest` separates `parse` from `load`. `peers` and `run`
are the two that open a connection, and nothing here opens a socket.
"""

from screener.scoring.adjust import Action, adjusted_closes

__all__ = ["Action", "adjusted_closes"]
