"""Shared agent configuration and harness primitives."""

from .agent_config import AgentConfigError, ResolvedAgent, load_config, resolve_role
from .harness import HarnessResult, run_harness, write_run_record
from .plane import PlaneConfig, PlaneError
from .topics import GuideError, TopicContext, TopicResult, serve_topic

__all__ = [
    "AgentConfigError",
    "GuideError",
    "HarnessResult",
    "PlaneConfig",
    "PlaneError",
    "ResolvedAgent",
    "TopicContext",
    "TopicResult",
    "load_config",
    "resolve_role",
    "run_harness",
    "serve_topic",
    "write_run_record",
]
