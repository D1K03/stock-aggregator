import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str


def settings() -> Settings:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return Settings(database_url=url)
