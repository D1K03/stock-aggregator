"""Runtime configuration, read from the environment.

Only what every process needs lives here — which is the database URL and
nothing else. Credentials belong to the subsystem that uses them:
`screener.fetch.config`, `screener.ai.config`, `screener.notify.config`. That
split is not tidiness. A single settings object forces one policy on every
field, so a required `DATABASE_URL` would make a process that only wanted to
post a Discord alert fail for want of a database it never touches.

Anything tunable at runtime — pillar weights, alert thresholds, cooldowns,
coverage floors — is a database row instead, so it can be changed without a
redeploy.
"""

from screener.config.settings import Settings, settings

__all__ = ["Settings", "settings"]
