"""Bright Data credentials, owned by the fetch layer."""

from dataclasses import dataclass, field

from screener.config import env

DEFAULT_PROXY_HOST = "brd.superproxy.io"
DEFAULT_PROXY_PORT = 44445


@dataclass(frozen=True)
class ProxyConfig:
    """Bright Data settings. Every field is optional.

    Absence is the normal case and means "not configured", never "broken":
    that is what makes the proxy strategies unreachable by default rather than
    disabled by a flag someone has to remember to set.
    """

    proxy: str | None = field(default=None, repr=False)
    host: str = DEFAULT_PROXY_HOST
    port: int = DEFAULT_PROXY_PORT
    user: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    unlocker_zone: str | None = None

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        return cls(
            # The combined `host:port:user:pass` form their dashboard hands
            # out. It wins over the discrete fields when both are present.
            proxy=env.optional("BRIGHTDATA_PROXY"),
            host=env.text("BRIGHTDATA_PROXY_HOST", DEFAULT_PROXY_HOST),
            port=env.integer("BRIGHTDATA_PROXY_PORT", DEFAULT_PROXY_PORT),
            user=env.optional("BRIGHTDATA_PROXY_USER"),
            password=env.optional("BRIGHTDATA_PROXY_PASS"),
            api_key=env.optional("BRIGHTDATA_API_KEY"),
            unlocker_zone=env.optional("BRIGHTDATA_UNLOCKER_ZONE"),
        )

    @property
    def unlocker_enabled(self) -> bool:
        return self.api_key is not None and self.unlocker_zone is not None

    def proxy_url(self, session: str | None = None) -> str | None:
        """The proxy URL, or None when no credentials are set."""
        host, port = self.host, self.port
        user, password = self.user, self.password

        if self.proxy:
            # Split at most three times: the password may contain colons.
            parts = self.proxy.strip().split(":", 3)
            if len(parts) == 4:
                host, port_text, user, password = parts
                try:
                    port = int(port_text)
                except ValueError as exc:
                    raise RuntimeError(
                        "BRIGHTDATA_PROXY port is not an integer; expected "
                        "host:port:user:pass"
                    ) from exc

        if not user or not password:
            return None

        # Bright Data selects an exit IP from the session suffix on the
        # username: the same suffix returns the same IP, a new one draws again.
        # A username that already pins a session is left alone — it was written
        # that way on purpose.
        if session and "-session-" not in user:
            user = f"{user}-session-{session}"
        return f"http://{user}:{password}@{host}:{port}"
