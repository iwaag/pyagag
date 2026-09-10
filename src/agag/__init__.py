"""Shared agent configuration and harness primitives.

`agag.plane` is deliberately **not** re-exported here. It was, and that made
`import agag.selfnote` load the Plane HTTP client — so an agent whose whole
record lives in Zulip (`refactor` p1) could not honestly say it never reaches
Plane, however carefully its own modules were written. Nothing outside
`agag.plane` and its tests ever used the two names from here; the consumers
that still speak to Plane import that module by name, which is what an
optional dependency should look like.
"""

from .agent_config import AgentConfigError, ResolvedAgent, load_config, resolve_role
from .harness import HarnessResult, run_harness, write_run_record
from .topics import GuideError, TopicContext, TopicResult, serve_topic

__all__ = [
    "AgentConfigError",
    "GuideError",
    "HarnessResult",
    "ResolvedAgent",
    "TopicContext",
    "TopicResult",
    "load_config",
    "resolve_role",
    "run_harness",
    "serve_topic",
    "write_run_record",
]
