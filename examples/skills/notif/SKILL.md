---
name: notif
description: >
  READ THIS BEFORE DECIDING WHAT REACHES THE OWNER'S PHONE UNASKED (a notification
  card, a recap, a done-notification). Loaded by the notification classifier
  (notif/daemon.js) on every batch. Compiled from NOTIF-CONTRACT.md; never edit by
  hand.
compiled: 2026-10-06
skill_v: 2026-10-06a
ledger: NOTIF-CONTRACT.example.md
rules_in_ledger: 7
rules_in_front: 5
---

# Notif (compiled from the ledger, 2026-10-06)

## How to use this

1. The reader is the notification classifier: notif/daemon.js appends this file to its system prompt on every call (env NOTIF_SKILL, default $SODA_SKILLS_DIR/notif/SKILL.md).
2. A sentence of feedback on a notification (a Telegram reply starting with `notif:`) is queued for the feedback session and filed in NOTIF-CONTRACT.md; this file is its compile.
3. In front: what the hit counts say is still needed. Assumed: still checked, back in front the moment it fires.

## The rules in front

### What a card shows

**R1.** Every line of a card names who wrote it (and the group, for a group chat) before the snippet. *e.g.* "Sam / Project chat: can we move the review to Monday?", never a bare snippet [ledger H1]

### What counts as news

**R2.** A message the owner answered later in the same thread is not news; a pushed card whose people were all answered collapses in place to one line. *e.g.* a question at 09:10 and the owner's reply at 09:30: no line; the 09:05 card becomes "answered by you at 09:30, closed" [ledger H2,H3]

**R3.** Never surface a message older than 48 hours as new (stale_hours 48). *e.g.* a poller that was down for three days does not replay its backlog as notifications [ledger H4]

**R4.** An introduction between the owner and a third party always needs a reply. *e.g.* subject "A <> B": kept even without a question in the preview [ledger H7]

### What never becomes a card

**R5.** Pure closure (ok, thanks, a reaction, an emoji) never becomes a card. *e.g.* "thanks!" after a sent file: dropped [ledger H5]

## Assumed (still checked by the observer, not in front)

- A fault a machine can repair goes to the system agent, never to the owner as a card. [ledger H6]

## Procedure

1. Drop what the owner already answered (R2) and what is stale (R3) before anything else.
2. Drop pure closure (R5).
3. Every kept line names who wrote it (R1).
