"""Steven's tools.

    from screener.bot.tools import specs, dispatch

`specs()` is offered to the model on each request; `dispatch()` runs whatever it
picks. To add a tool, write a function in a module here, decorate it with
`@tool(name, description)`, and import the module below so the decorator runs.
That import is the whole registration step.
"""

from screener.bot.tools import deployment as _deployment  # noqa: F401  (registers)
from screener.bot.tools.registry import MAX_RESULT, TOOLS, Tool, dispatch, specs, tool

__all__ = ["MAX_RESULT", "TOOLS", "Tool", "dispatch", "specs", "tool"]
