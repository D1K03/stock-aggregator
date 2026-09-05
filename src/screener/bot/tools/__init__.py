"""Steven's tools.

    from screener.bot.tools import specs, dispatch

`specs()` is offered to the model on each request; `dispatch()` runs whatever it
picks. To add a tool, write a function in a module here, decorate it with
`@tool(name, description)`, and import the module below so the decorator runs.
That import is the whole registration step.

Most tools answer a question. A few *do* something — `watch` and `hold` put a
live stream capture on and take it off — and those want to know who asked, which
`acting()` supplies for the same reason `collecting()` supplies a chart: the
model must not be told, because a name in its arguments is a name it could
invent.

A tool can also produce something to *show* rather than to say. `collecting()`
gathers those artifacts — charts today — so they can travel beside the reply
instead of through the model's context, where a chart would cost more than it
could ever be worth as text.
"""

from screener.bot.tools import charts as _charts  # noqa: F401  (registers)
from screener.bot.tools import deployment as _deployment  # noqa: F401  (registers)
from screener.bot.tools import skybird as _skybird  # noqa: F401  (registers)
from screener.bot.tools.charts import Chart, Mark, collecting
from screener.bot.tools.registry import (
    MAX_RESULT,
    TOOLS,
    Tool,
    acting,
    actor,
    dispatch,
    specs,
    tool,
)

__all__ = [
    "MAX_RESULT", "TOOLS", "Chart", "Mark", "Tool",
    "acting", "actor", "collecting", "dispatch", "specs", "tool",
]
