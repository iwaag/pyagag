"""A fixture board: a synthetic realm a guide trial reads (`agent_guide` p2 step 6).

p1's before-trial did not reproduce run-0160, because the live board had
changed since (a topic the old guide happened to find existed only after the
incident). A controlled comparison needs the same board every time, so this
package builds one **in code** — no post of it was exported from the realm —
into a mirror store a real run's `agentchat` reads through `AGENTCHAT_MIRROR`
(`agag.mirror.reads`). The store says it is a fixture, so a run pointed at it
cannot post anywhere: every write and every read beyond the board fails with
a line naming the fixture.

    python -m agag.fixture build <dir>        # <dir>/mirror.sqlite, rebuilt from scratch
    python -m agag.fixture probes             # the probes and their pass rules
    python -m agag.fixture check <probe> <reply file>   # a reply against its rule

`board.py` is the realm (projects and studies, routines with runs, archsage's
topics, every agent's introduction, forge's past work, one ✔'d past request);
`probes.py` is what is asked of it and how an answer is judged; `run.py`
serves one probe to a role the way its listener's first serving would, with
a given guide, against the fixture.
"""

from .board import FIXTURE_NAME, Board, build_board, build_store
from .probes import PROBES, Probe, judge

__all__ = ["FIXTURE_NAME", "PROBES", "Board", "Probe", "build_board", "build_store", "judge"]
