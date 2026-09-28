"""Guide text several agents share, written once (`agent_guide` p2 step 3).

Each `<name>.md` here is one section a role can be given after its own
guide: `agag.topics.prompt_with_guide(…, shared=("board", "refs"))`. The
agent's code names the sections per role; the text lives here and nowhere
else. `entrance.md` and `argue_participant.md` are the fixed parts of two
compositions pyagag makes itself (`agag.entrance`, `agag.argue`).
"""
