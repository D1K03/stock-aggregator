"""Daily bars into percentiles, a pillar score and a dated snapshot.

Five of the seven modules are pure -- they take plain values and return plain
values -- as `screener.ingest` separates `parse` from `load`. `peers` and `run`
are the two that open a connection, and nothing here opens a socket.
"""

from screener.scoring.adjust import Action, adjusted_closes
from screener.scoring.blend import AGREEMENT_THRESHOLD, Snapshot, blend
from screener.scoring.metrics import CODES, compute, months_before
from screener.scoring.percentile import deciles, percentiles
from screener.scoring.peers import MIN_PEERS, Peer, resolve
from screener.scoring.pillars import PillarScore, score_pillar
from screener.scoring.run import (
    BAR_WINDOW_MONTHS,
    CUTOFF_OFFSET,
    active_securities,
    read_actions,
    read_bars,
    visibility_cutoff,
)

__all__ = ["AGREEMENT_THRESHOLD", "BAR_WINDOW_MONTHS", "CODES", "CUTOFF_OFFSET", "MIN_PEERS", "Action", "Peer", "PillarScore", "Snapshot", "active_securities", "adjusted_closes", "blend", "compute", "deciles", "months_before", "percentiles", "read_actions", "read_bars", "resolve", "score_pillar", "visibility_cutoff"]
