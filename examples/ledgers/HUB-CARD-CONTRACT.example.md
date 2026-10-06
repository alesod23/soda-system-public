# Hub card contract (EXAMPLE ledger, v1)

**Why this file exists.** Every action that leaves the system (an email, a chat message, a post) or that only the owner
can decide becomes a card in the approval hub, answered from the phone. This file is the rule ledger of that surface:
every sentence of feedback the owner ever gave about cards, verbatim, dated, append-only. The hub skill is compiled
from it (`rules/compile_skill.py --contract hub`) and the sessions that post cards read the skill, not this file.

**This is an example.** The rules and quotes below are invented to show the format. Replace them with your own, or
keep them as a starting point and let your feedback grow the table.

**HARD** = never violate (an observer may block, downgrade or regenerate). **SOFT** = judgement. Every rule cites its
source so a wrong rule is fixed upstream instead of patched here.

**To add a rule the moment it is given:**
```
python rules/addrule.py --contract hub --rule "..." --quote "the exact words" [--soft] [--item <card id>]
python rules/addrule.py --contract hub --like --quote "the card text that was liked" --item <card id> [--maps-to H3]
python rules/addrule.py --contract hub --rule "..." --quote "..." --suppress "<regex>"   # also a live filter in the hub
```

**Anatomy of a row.** `| H<n> | <the rule, imperative> *"<the owner's words, verbatim>"* [item: <artifact id>] | <source> |`.
The id never changes once given. A `LIKED:` row is a positive example; `[maps-to: H<n>]` names the rule it satisfies.

## HARD - when a card exists at all

| # | Rule | Source |
|---|---|---|
| H1 | **Anything outbound to another human needs a card.** Email, chat, social. The session writes the draft; the hub sends it on the owner's yes. A session never sends to a third party itself. *"nothing goes out to a person without me tapping yes"* | owner ruling 2026-01-08 |
| H2 | **Never ask to confirm what was already instructed.** A change the owner ordered in another session is reported as an update (`kind: update`), not asked again. *"I told you to do it an hour ago, don't make me approve it twice"* | owner ruling 2026-01-12, filed with --suppress |
| H3 | **A card pings once.** A re-post about the same subject (same `idem` key) edits the existing card in place, silently; a new card from the same producer about the same person and action supersedes the old one. *"one subject, one card, one buzz"* | owner ruling 2026-01-15 |

## HARD - what a card must contain

| # | Rule | Source |
|---|---|---|
| H4 | **The first line says whether it is a decision or an update, and what.** Top-down: line one tells what is being approved. *"sometimes there's nothing to approve and the card still asks me"* | owner ruling 2026-01-09 |
| H5 | **The artifact is on the card.** A draft card carries the whole draft (collapsed), the recipient and the subject; never "see the laptop". *"I can't approve an email I can't read"* | owner ruling 2026-01-10 |
| H6 | **One card, one decision.** Two unrelated asks are two cards. *"don't bundle three questions, I can only answer yes or no"* | owner ruling 2026-01-20 |
| H7 | **SOFT, flag only.** Say why now. A card that is not urgent says when it would become urgent. *"why is this reaching me at 11pm?"* | owner ruling 2026-01-22 |
| H8 | LIKED: this card text was right as written. *"Send the follow-up to the supplier with the revised quote attached? Draft below, sent from the work account."* [item: a1b2c3] [maps-to: H4] | owner liked it 2026-01-25 |

## SOFT - judgement

| Rule | Source |
|---|---|
| A card is a proposal, not state: before saying something is "pending", check the sent folder and the calendar. | design note |
| Keep a card body under one phone screen; the hub truncates long context. | design note |

## Where the code is

- `hub/server.js`: `POST /pending` creates (the guard applies `$SODA_HOME/hub/hub-rules.json` first), `POST /resolve`
  decides and runs the action on yes, `POST /revise` edits in place, `POST /close` resolves without running anything.
- `rules/addrule.py --suppress`: one sentence becomes a written rule and a live filter in the same call.
