"""What every process needs, and nothing else."""

from dataclasses import dataclass, field

from screener.config import env


@dataclass(frozen=True)
class Settings:
    """Process-wide configuration.

    One field. New credentials do not land here — see the package docstring for
    why. `repr=False` because a connection string carries a password and a
    settings object caught in a traceback should not print it.
    """

    database_url: str = field(repr=False)


def settings() -> Settings:
    """Read settings from the environment.

    Uncached, and read fresh on every call rather than held in a module-level
    singleton, because `screener.secrets.load_into_environ()` mutates
    `os.environ` during boot. A cached object would freeze whatever the first
    caller happened to see, so a stray import-time `settings()` anywhere in the
    tree would silently pin pre-Infisical values — and the symptom would be a
    wrong database, not an error.
    """
    return Settings(database_url=env.required("DATABASE_URL"))
