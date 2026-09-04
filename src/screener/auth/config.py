"""Credentials and the allow-list, owned by the auth layer."""

from dataclasses import dataclass, field

from screener.config import env


@dataclass(frozen=True)
class AuthConfig:
    """OAuth application details and who is permitted to sign in.

    Sign-in is inert without a client id, secret and session secret: the login
    route reports that it is not configured rather than half-working. That keeps
    local runs and the test suite from needing a real OAuth application.
    """

    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)
    allowed_logins: frozenset[str] = frozenset()
    base_url: str = "http://localhost:8080"
    session_days: int = 30

    @classmethod
    def from_env(cls) -> "AuthConfig":
        raw = env.optional("ALLOWED_GITHUB_LOGINS") or ""
        return cls(
            client_id=env.optional("GITHUB_CLIENT_ID"),
            client_secret=env.optional("GITHUB_CLIENT_SECRET"),
            session_secret=env.optional("SESSION_SECRET"),
            # GitHub logins are case-insensitive and it renders them however
            # the owner typed them, so the allow-list is folded on both sides.
            allowed_logins=frozenset(
                part.strip().lower() for part in raw.split(",") if part.strip()
            ),
            base_url=env.text("APP_BASE_URL", "http://localhost:8080").rstrip("/"),
            session_days=env.integer("SESSION_DAYS", 30),
        )

    @property
    def enabled(self) -> bool:
        return (
            self.client_id is not None
            and self.client_secret is not None
            and self.session_secret is not None
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/auth/callback"

    def permits(self, login: str) -> bool:
        """Whether this GitHub login may sign in.

        An empty allow-list permits nobody. The alternative reading, that
        unset means everyone, turns a forgotten variable into an open door.
        """
        return login.strip().lower() in self.allowed_logins
