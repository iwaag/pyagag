"""Shared agent configuration and harness primitives."""

from .agent_config import AgentConfigError, ResolvedAgent, load_config, resolve_role
from .harness import HarnessResult, run_harness, write_run_record

__all__ = [
    "AgentConfigError",
    "HarnessResult",
    "ResolvedAgent",
    "load_config",
    "resolve_role",
    "run_harness",
    "write_run_record",
]
