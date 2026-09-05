"""One row per person, rather than one per identity they use.

`by_actor` groups on what the trail actually stored — a GitHub login from the
dashboard, a Discord user id from the server — and those are two rows for one
person. Folding them needs a mapping from one to the other, and that mapping
exists: `DISCORD_USER_MAP` already links them so a conversation can be handed
over. This is the same mapping read the other way round.

Kept as a pure function over a supplied mapping rather than reading the
environment itself, so the audit layer does not acquire an opinion about
Discord configuration, and so a test can fold whatever pairing it likes.

Anyone unmapped still gets a row. They appear under the identity the trail has,
marked `known=False`, because a bare Discord id is a worse answer than dropping
them but a much better one than silently attributing their spend to nobody.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from screener.audit.models import ActorSpend


@dataclass(frozen=True, slots=True)
class Person:
    """What one human cost, across every surface they used."""

    login: str
    #: Whether `login` is a GitHub login, and so whether an avatar exists.
    known: bool
    events: int
    cost: Decimal
    tokens: int
    cost_24h: Decimal
    last_seen: datetime
    #: Per surface, dearest first, so a row can show where the money went.
    surfaces: tuple[tuple[str, Decimal], ...]


def fold(
    rows: Sequence[ActorSpend], identities: Mapping[str, str]
) -> list[Person]:
    """Merge identities belonging to the same person, dearest first.

    `identities` maps a Discord user id to a GitHub login. GitHub logins are
    case-insensitive and a session carries whatever casing its owner chose, so
    everything is matched folded and the display name is whichever spelling the
    trail recorded.
    """
    merged: dict[str, dict] = {}

    for row in rows:
        mapped = identities.get(row.actor)
        if row.actor_kind == "github":
            key, name, known = row.actor.casefold(), row.actor, True
        elif mapped:
            key, name, known = mapped.casefold(), mapped, True
        else:
            # No mapping: keep them, under whatever the trail has. Prefixed by
            # kind so a Discord id can never collide with a GitHub login.
            key, name, known = f"{row.actor_kind}:{row.actor}", row.actor, False

        person = merged.setdefault(
            key,
            {
                "login": name, "known": known, "events": 0,
                "cost": Decimal(0), "tokens": 0, "cost_24h": Decimal(0),
                "last_seen": row.last_seen, "surfaces": {},
            },
        )
        # A GitHub row names the person better than a mapping does, since it is
        # the spelling they actually sign in with.
        if row.actor_kind == "github":
            person["login"], person["known"] = row.actor, True
        person["events"] += row.events
        person["cost"] += row.cost
        person["tokens"] += row.tokens
        person["cost_24h"] += row.cost_24h
        person["last_seen"] = max(person["last_seen"], row.last_seen)
        person["surfaces"][row.actor_kind] = (
            person["surfaces"].get(row.actor_kind, Decimal(0)) + row.cost
        )

    people = [
        Person(
            login=p["login"],
            known=p["known"],
            events=p["events"],
            cost=p["cost"],
            tokens=p["tokens"],
            cost_24h=p["cost_24h"],
            last_seen=p["last_seen"],
            surfaces=tuple(sorted(p["surfaces"].items(), key=lambda s: -s[1])),
        )
        for p in merged.values()
    ]
    people.sort(key=lambda p: p.cost, reverse=True)
    return people


# The pseudo-login `/auth/local` issues. Not a GitHub account — except that
# somebody holds github.com/local-dev, so asking for their picture puts a
# stranger's face on the spend panel during local development.
LOCAL_LOGIN = "local-dev"


def avatar(person: Person) -> str | None:
    """Their GitHub picture, or `None` when there is no GitHub identity.

    github.com serves this without an API call or a token, which is why it is a
    URL rather than something fetched and cached here. It does mean the
    browser tells GitHub who it is looking at; that is a dashboard listing
    GitHub logins, so it is not learning much.
    """
    if not person.known or person.login == LOCAL_LOGIN:
        return None
    return f"https://github.com/{person.login}.png?size=80"
