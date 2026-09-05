"""Steven's tools.

    from screener.bot.tools import specs, dispatch

`specs()` is offered to the model on each request; `dispatch()` runs whatever it
picks. To add a tool, write a function in a module here, decorate it with
`@tool(name, description)`, and import the module below so the decorator runs.
That import is the whole registration step.

A tool can also produce something to *show* rather than to say. `collecting()`
gathers those artifacts — charts today — so they can travel beside the reply
instead of through the model's context, where a chart would cost more than it
could ever be worth as text.
"""

from screener.bot.tools import charts as _charts  # noqa: F401  (registers)
from screener.bot.tools import deployment as _deployment  # noqa: F401  (registers)
from screener.bot.tools import query as _query  # noqa: F401  (registers)
from screener.bot.tools.charts import Chart, Mark, collecting
from screener.bot.tools.query import Rows, collecting_rows
from screener.bot.tools.registry import MAX_RESULT, TOOLS, Tool, dispatch, specs, tool

__all__ = [
    "MAX_RESULT", "TOOLS", "Chart", "Mark", "Tool",
    "Rows",
    "collecting", "collecting_rows", "dispatch", "specs", "tool",
]
