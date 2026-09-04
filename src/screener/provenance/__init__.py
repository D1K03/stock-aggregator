"""Where a run came from: which build produced it, and under what config.

`scoring_run.git_sha` and `scoring_run.config_hash` are both `not null` and
neither had a producer. These are those producers.
"""

from screener.provenance.git import GIT_SHA_ENV, git_sha, require_git_sha
from screener.provenance.hashing import config_hash

__all__ = ["GIT_SHA_ENV", "config_hash", "git_sha", "require_git_sha"]
