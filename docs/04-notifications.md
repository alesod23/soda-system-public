# 4. The notification loop

What reaches the owner's phone unasked is decided by one daemon (`notif/daemon.js`) and governed by one ledger
(`NOTIF-CONTRACT.md`, see `examples/ledgers/NOTIF-CONTRACT.example.md`). It is the rule loop of
[01-rule-loop.md](01-rule-loop.md) applied to notifications, end to end.

```
 pollers you already have            notif/daemon.js                                       the phone
 (mail, chat, ...)                   ---------------                                       ---------
        |                                                                                    
        v                                                                                    
 store JSONL ---> sources/jsonl.js ---> debounce ---> filters (code) ---> classify (model) ---> card ---> push.js
 (one per channel)   new lines only     one batch     answered / stale /   prompt = contract    who wrote,    Telegram
                                        per burst     already shown        + COMPILED notif     group, when,  Bot API
                                                          |                skill, read on        snippet,
                                                          v                every batch           draft reply
                                                    rule-hits.jsonl <------ notif-observer.js <---+
                                                          ^                                       |
                                                          |                                       v
                                                    notif-collapse.js tick: a card the owner has since answered is
                                                    edited IN PLACE into one line "answered by you at HH:MM"

 the owner's sentence "notif: this should never reach me" ---> hub POST /feedback {surface: "notif"}
   ---> feedback session ---> ledger_verdict.py ---> addrule.py --contract notif ---> compile_skill.py
   ---> data/skills/notif/SKILL.md ---> read by the classifier on its NEXT batch
```

## The pieces

| File | Job |
|---|---|
| `notif/sources/jsonl.js` | Tails one JSONL store per channel (`NOTIF_STORES="Email=...,Chat=..."`). The line format is the only contract with your pollers: `{id, channel, thread, sender, group, text, ts, from_me}`. Lines with `from_me: true` are never notified, but they prove a thread was answered. |
| `notif/debounce.js` | Per-channel quiet window (`NOTIF_DEBOUNCE_MS`, default 8 s) with a hard cap from the first message (`NOTIF_MAXWAIT_MS`, 22 s). A burst of five messages is one card. |
| `notif/notif-collapse.js` `filterBatch` | Code filters before any model call: a message the owner already answered (a later `from_me` line in the same thread), one older than the stale window (rule H4: the number lives in the ledger, `stale_hours <n>`, parsed from the compiled skill on every call), one whose text already sat on a card. |
| `notif/daemon.js` `classify` | One model call per batch through the Claude Code CLI (`SODA_LLM_BIN`, `SODA_LLM_MODEL`). The system prompt is a fixed I/O contract plus the compiled notif skill, re-read from disk every time. `SODA_LLM_STUB=<file>` answers from a file (tests, offline). |
| `notif/daemon.js` `buildCard` | Every line names who wrote it and the group (H1); the suggested reply is a draft in italics and is never sent. |
| `notif/notif-log.js` | Append-only log of every card (`data/notif/notification-log.jsonl`) with the Telegram message id, so a card can be edited later and a session can answer "what was that card about?". |
| `notif/notif-observer.js` | The observer: one hit line per rule per card into `rule-hits.jsonl` (H1 ok/flag, H2 block/ok, H4 block). Never throws, never blocks, never edits. |
| `notif/notif-collapse.js` `tick` | Every `NOTIF_COLLAPSE_EVERY_MS` (2 min): a pushed card whose every sender the owner answered after their message is edited in place into one line (H3). It never sends a new message; after 3 failed edits it logs an H3 `flag`. |
| `notif/push.js` | Telegram Bot API, owner's chat only (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Without a token it prints the card and returns `message_id=0`, so everything runs offline. |

## Why the filters are in code and the judgement is in the model

"Did the owner already answer?" and "is it older than the owner's window?" have exact answers in the store, so they never cost a model
call and never get a model's guess. "Is this worth the owner's attention?" has no exact answer, so it goes to the model, and
the model is told the owner's rules in their own words through the compiled skill. When a filter fires it still writes a
hit line, so the compiler sees that the rule is alive.

## Run it

```bash
mkdir -p data/notif
NOTIF_STORES="Chat=./data/notif/chat-store.jsonl" node notif/daemon.js        # log only
echo '{"id":"1","thread":"t1","sender":"Sam Example","text":"Can you review the draft today?","ts":"'$(date -u +%FT%TZ)'","from_me":false}' >> data/notif/chat-store.jsonl
NOTIF_PUSH=1 node notif/daemon.js                                              # push for real
cd notif && npm test                                                               # debounce + end-to-end self-test
```

## Giving feedback

The owner never edits the ledger. A sentence that judges a notification ("this should never reach me", "good one, say who
wrote it") is posted to the hub's `POST /feedback {surface: "notif", item: <card id>, line: <the card text>, text: <their
words>}` (from a Telegram reply prefixed `notif:`, or a feedback box on a page). The feedback session classifies it
(like, confirmation, rule, case, system, work), files a rule with `addrule.py --contract notif`, recompiles the skill,
and the classifier reads the new skill on its next batch.
