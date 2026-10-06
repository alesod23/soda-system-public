# Email review contract (EXAMPLE ledger, v1)

**Why this file exists.** Every message written on the owner's behalf (email, chat, social DM) is drafted by a session
that has read the compiled drafting skill, checked after the fact by a critic, and sent only by the hub on the owner's
yes. This file is the rule ledger of how those messages are written.

**This is an example.** The rules and quotes below are invented to show the format.

**To add a rule the moment it is given:**
```
python rules/addrule.py --contract email --rule "..." --quote "the exact words" [--soft] [--item <draft id>]
python rules/addrule.py --contract email --like --quote "the sentence that was liked" --item <draft id> [--maps-to H2]
```

## HARD - structure

| # | Rule | Source |
|---|---|---|
| H1 | The ask goes first, subject included: the reader knows what is wanted from line one. *"put the ask at the top, I don't want them to scroll for it"* | owner ruling 2026-01-04 |
| H2 | At most five sentences for a first message. *"if it doesn't fit on a phone screen nobody reads it"* | owner ruling 2026-01-04 |
| H3 | Reply in the existing thread; never start a new thread with someone who already wrote. *"keep it in the thread so they have the context"* | owner ruling 2026-01-06 |

## HARD - voice

| # | Rule | Source |
|---|---|---|
| H4 | No filler openers ("I hope this finds you well", "just checking in"). *"sounds like a template, delete it"* | owner ruling 2026-01-08 |
| H5 | Write in the language the person last wrote in. *"she wrote in Spanish, answer in Spanish"* | owner ruling 2026-01-10 |
| H6 | Never claim an attachment or a link that is not actually there. *"you said 'attached' and there was nothing attached"* | owner ruling 2026-01-12 |
| H7 | **SOFT, flag only.** One concrete proposed time beats "let me know when you're free". *"give them a slot, not homework"* | owner ruling 2026-01-14 |
| H8 | LIKED: the opening was right as written. *"Could we move Thursday's call to 15:00? Something came up at 14:00 on my side."* [item: r-example-1] [maps-to: H1] | owner liked it 2026-01-16 |

## SOFT - judgement

| Rule | Source |
|---|---|
| Match the formality of the person's last message. | design note |

## Where the code is

- The drafting sessions read the compiled skill (`$SODA_SKILLS_DIR/drafting/SKILL.md`).
- A draft becomes a hub card (`hub/server.js`, card `type: email-draft`, the action names the draft id); the hub sends
  it on yes through the configured executor.
