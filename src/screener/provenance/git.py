"""The commit a process was built from."""

import os
import subprocess

# Baked into the image at build time from the CD workflow's `--build-arg`. A
# container has no git history, so this is the only way it can know.
GIT_SHA_ENV = "SCREENER_GIT_SHA"

UNKNOWN = "unknown"


def _from_git() -> str | None:
    """The working tree's HEAD, or None outside a checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_sha() -> str:
    """The build's commit, or "unknown". Never raises.

    Lenient because `/status` has to answer even from a container someone built
    by hand without the build argument — an endpoint that 500s is worse at
    telling you what is running than one that admits it does not know.
    """
    return os.environ.get(GIT_SHA_ENV) or _from_git() or UNKNOWN


def require_git_sha() -> str:
    """The build's commit, or a RuntimeError.

    Strict, for anything writing `scoring_run.git_sha`. That table is
    append-only and the column exists for reproducibility, so a row stamped
    "unknown" is a permanent, unrepairable lie about a run — worse than the run
    not happening. Two names rather than a flag, because a boolean that
    switches between raising and not raising reads as an accident at the call
    site.
    """
    sha = os.environ.get(GIT_SHA_ENV) or _from_git()
    if not sha:
        raise RuntimeError(f"{GIT_SHA_ENV} is not set and this is not a git checkout")
    return sha
