"""Shared agent configuration and harness primitives.

`agag.plane` is gone (`refactor` p3). It was re-exported here once, which made
`import agag.selfnote` load a Plane HTTP client — so an agent whose whole
record lives in Zulip could not honestly say it never reaches Plane, however
carefully its own modules were written. p1 and p2 moved autolab's and forge's
records into the conversations, p3 moved cagent's, and the client outlived its
last consumer. What it carried that was never about Plane — splitting a
Markdown file into a title and a body — is `agag.document`.
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
