# Notification contract (EXAMPLE ledger, v1)

**Why this file exists.** The notification layer decides what reaches the owner's phone unasked: one card per pooled
batch of incoming messages, with who wrote, when, the snippet and a suggested reply (a draft, never sent). This file is
the rule ledger of that surface. The classifier (`notif/daemon.js`) reads the COMPILED skill on every batch, so a rule
filed here is live on the next batch after the compile.

**This is an example.** The rules and quotes below are invented to show the format.

**HARD** = never violate. **SOFT** = judgement. Every rule cites its source.

**To add a rule the moment it is given** (or reply `notif: <sentence>` to a notification on Telegram, which the
feedback session files for you):
```
python rules/addrule.py --contract notif --rule "..." --quote "the exact words" [--soft] [--item <notification id>]
```

**What this governs:** everything that arrives on the phone unasked. It does NOT govern approval cards (that is
`HUB-CARD-CONTRACT.md`).

## HARD

| # | Rule | Source |
|---|---|---|
| H1 | Every line of a card names who wrote it (and the group, for a group chat) before the snippet. *"a snippet without a name is useless, I have to open the app anyway"* | owner feedback on a notification 2026-01-11 |
| H2 | A conversation the owner already answered is not news: when the owner wrote later in the same thread, the message never becomes a line. *"I replied to that one from my laptop, why are you showing it?"* | owner feedback 2026-01-11 |
| H3 | A pushed card whose every person the owner answered after it collapses in place to one line ("answered by you at HH:MM, closed"), never a second message. *"once I've answered, shrink it so I can see what's left"* | owner ruling 2026-01-14 |
| H4 | Never surface a message older than 48 hours as new: stale_hours 48. *"a two-day-old message is not a notification, it's a backlog"* | owner ruling 2026-01-16 |
| H5 | Pure closure (ok, thanks, a reaction, an emoji) never becomes a card. *"don't wake me up for a thumbs up"* | owner ruling 2026-01-17 |
| H6 | A fault a machine can repair goes to the system agent, never to the owner as a card asking to act. *"if the poller logged out, fix it, don't tell me to"* | owner ruling 2026-01-19 |
| H7 | **SOFT, flag only.** An introduction between the owner and a third party always needs a reply, even when the preview has no question. *"intros are always actionable"* | owner ruling 2026-01-21 |

## SOFT (judgement)

| Rule | Source |
|---|---|
| A suggested reply is short, in the owner's voice, and a draft only. | design note |

## Where the code is

- `notif/daemon.js`: debounce per channel, the answered and stale filters, the classifier (reads the compiled notif
  skill on every call), the card, the push.
- `notif/notif-observer.js`: one hit line per rule checked on a rendered card (H1, H2).
- `notif/notif-collapse.js`: the collapse tick (H3).
