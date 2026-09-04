"""OpenRouter credentials, owned by the AI layer."""

from dataclasses import dataclass, field

from screener.ai.models import DEFAULT_MODEL
from screener.config import env


@dataclass(frozen=True)
class RouterConfig:
    """OpenRouter settings.

    The whole layer is inert without a key rather than broken by its absence,
    so `api_key` is optional and callers check `enabled` first.
    """

    api_key: str | None = field(default=None, repr=False)
    model: str = DEFAULT_MODEL
    app_base_url: str = "http://localhost:8080"

    @classmethod
    def from_env(cls) -> "RouterConfig":
        return cls(
            api_key=env.optional("OPENROUTER_API_KEY"),
            model=env.text("AI_MODEL", DEFAULT_MODEL),
            app_base_url=env.text("APP_BASE_URL", "http://localhost:8080"),
        )

    @property
    def enabled(self) -> bool:
        return self.api_key is not None
