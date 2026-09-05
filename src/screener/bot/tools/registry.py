"""The tool registry Steven calls into.

Adding a tool is one function and one decorator. Nothing else in the bot needs
touching: `specs()` is what the model is offered and `dispatch()` is what runs,
both derived from the same registration, so the list the model sees and the
code that executes cannot drift apart.

Everything here is deliberately terse, because every character of a tool name,
description and result is a token paid for on every single message. A tool that
describes itself in a sentence costs more per conversation than the tool saves.
"""

import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# A tool result is context on the next request, so a chatty one is paid for
# twice: once to receive and once to send back. Anything longer than this is
# cut rather than allowed to dominate the conversation.
MAX_RESULT = 400

_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., str]


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a function as a tool.

    The parameter schema is derived from the signature rather than written out
    by hand, so a renamed argument cannot leave a stale schema behind telling
    the model to send something the function no longer takes.

    Keep the description to a short phrase. It is sent on every request.
    """

    def register(fn: Callable[..., str]) -> Callable[..., str]:
        if name in TOOLS:
            raise RuntimeError(f"a tool named {name!r} is already registered")

        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in inspect.signature(fn).parameters.values():
            json_type = _JSON_TYPES.get(parameter.annotation, "string")
            properties[parameter.name] = {"type": json_type}
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)

        TOOLS[name] = Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties, "required": required},
            run=fn,
        )
        return fn

    return register


def specs() -> list[dict[str, Any]]:
    """Every tool, in the shape the chat completions API expects."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS.values()
    ]


def dispatch(name: str, arguments: dict[str, Any]) -> str:
    """Run one tool and return its result as text. Never raises.

    A failure is handed back to the model as a short string rather than thrown,
    because the model can say "I could not check that" and carry on, whereas an
    exception ends the conversation with nothing for the person who asked.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return f"error: no tool named {name}"

    try:
        result = entry.run(**arguments)
    except TypeError as exc:
        # Wrong or missing arguments: the model's mistake, and one it can fix
        # on the next turn if it is told plainly.
        return f"error: bad arguments for {name}: {exc}"
    except Exception as exc:
        logger.warning("tool %s failed: %s", name, exc)
        return f"error: {name} failed: {type(exc).__name__}"

    text = result if isinstance(result, str) else json.dumps(result, separators=(",", ":"))
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + "…"
    logger.info("tool %s -> %d chars", name, len(text))
    return text
