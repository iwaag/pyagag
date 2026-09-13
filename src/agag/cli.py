"""`agag`: build and provision small agents on the shared skeleton.

`agentchat` (the chat CLI a run uses) is a separate command and stays so.
`agag wait` lives here rather than there for the same reason: it blocks, and
a run is one reply (`agag.notice`).
"""

from __future__ import annotations

import argparse
import sys

from .init import add_init_parser
from .notice import add_wait_parser
from .provision import add_provision_parser


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agag", description="agag agent tooling")
    sub = parser.add_subparsers(dest="command", required=True)
    add_init_parser(sub)
    add_provision_parser(sub)
    add_wait_parser(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
