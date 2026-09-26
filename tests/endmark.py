"""Reading a reply's words without its serving-end mark (failsafe p1).

Every reply a listener posts into the conversation it acknowledged now
ends its serving with `end=<ack id>` on its `ag-post` line. Tests about
something else — who is addressed, what intent survives a repair, what is
redelivered — compare the words and the meaning without it; the mark itself
is asserted where it is the subject (`test_post`, `test_serving_lifecycle`).
"""

import re
from dataclasses import replace

from agag.post import PostMeta

_ALONE = re.compile(r"\n\n`ag-post end=\d+`$")
_WORD = re.compile(r" end=\d+`")


def plain(value):
    if isinstance(value, PostMeta):
        return replace(value, end=None)
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, str):
        return _WORD.sub("`", _ALONE.sub("", value))
    return value
