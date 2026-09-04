"""Typed readers for environment variables.

Absence and emptiness are treated as the same thing. A variable exported as an
empty string is what a shell produces from an unset compose interpolation such
as `${BRIGHTDATA_PROXY:-}`, and treating that as a configured empty credential
would turn a missing secret into a confusing downstream failure instead of a
clear "not configured".
"""

import os


def optional(name: str) -> str | None:
    """The variable's value, or None if unset or empty."""
    value = os.environ.get(name, "").strip()
    return value or None


def required(name: str) -> str:
    """The variable's value, or a RuntimeError naming what is missing."""
    value = optional(name)
    if value is None:
        raise RuntimeError(f"{name} is not set")
    return value


def text(name: str, default: str) -> str:
    """The variable's value, or `default` if unset or empty."""
    return optional(name) or default


def integer(name: str, default: int) -> int:
    """The variable's value as an int, or `default` if unset or empty.

    A non-numeric value raises rather than silently falling back: a typo in a
    port number should stop the process, not route traffic somewhere else.
    """
    value = optional(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {value!r}") from exc
