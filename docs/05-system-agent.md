# 5. The system agent

A personal system that runs a dozen services across machines breaks quietly: a poller stops, a token expires, a port
stops answering. The system agent is the maintainer. Its design goal: the owner hears about a fault only when the owner's hand
is the one thing that can fix it, and then exactly once, with the fix one tap away.

```
 cron every 10 min
   system_agent.py check
      |
      |  1. CHECKS (no model)        http / tcp / file_fresh / systemd / command, each with a stable key
      v
   failing? ---- no ----> line open? --> one pass = verifying; N passes in a row = fixed (and its card closes)
      |
     yes
      |  2. WHITELIST FIX            the one self-repair the check declares (restart a unit, run a command),
      |                              capped per incident (fix_caps), spaced (fix_gap_min)
      v
   still failing after escalate_after_runs ticks, or owner_only?
      |
      |  3. STUCK                    ONE hub card "STUCK on <key>" (idempotent: pings once), whose text says what broke,
      v                              what it means, what was tried, and what YES and NO do
   the owner's YES  ---> unstuck.py tick runs the line's for_owner action once (argv, no shell), records exit + output
   the owner's NO   ---> recorded; nothing runs; no second card
```

## The registry

`data/system-agent/broken.jsonl` is append-only; the newest line of an id wins. A line carries `key` (the check),
`what`, `since`, `class`, `owner`, `status`, `attempts`, `last_action`, `next`, `evidence`, `card`, `for_owner`.

- `class maintenance`: the agent's own to-do.
- `class owner_command`: the fix is known but only the owner may run it (`owner_only: true` on the check): no
  self-repair, straight to a STUCK card with one action.
- `class owner_decision`: waiting for the owner's choice. Never "broken", never a card from the agent.

`system_agent.py registry --md FILE` renders the open lines as a markdown table, to include in any status page.

## The proof is the check, twice

A fix is not done because a command exited 0. It is done when the same check that failed passes
`registry_close_after_passes` times in a row (default 2), the second time in the agent's own later tick.
`system_agent.py check --only <key>` runs one check and exits 0/1: it is also what a repair session runs to reproduce
the fault and to prove its fix.

## Fix sessions and the never-list

A fault that needs reading code, not restarting a unit, can be handed to a model session. That session runs with
`fix_guard.py` as its PreToolUse hook: the never-list is enforced by code, not by trust.

| Never | Examples refused |
|---|---|
| sign in | `gcloud auth login`, an OAuth flow |
| send to a person | a mail, a chat message, a Bot API call, an MCP tool that sends |
| delete data | `rm`, `DROP TABLE`, `DELETE FROM`, `git reset --hard` (rename to `.bak` instead) |
| touch secrets or permissions | `.env`, credential files, settings files, `chmod`, `sudo` |
| write into read-only paths | anything under `SYSAGENT_READONLY_PATHS` (e.g. repos a sync job owns), any write over ssh |
| kill or open | `pkill`, `kill -9`, opening a window, a nested model session |

A blocked call returns exit 2 with the reason; the session must then stop and answer `stuck` with a `for_owner` action,
which becomes the STUCK card. `python3 system-agent/fix_guard.py --test "<command>"` shows the verdict for one command.

## The daily system update

`system_agent.py update` posts ONE hub card of kind `update` (told, not asked): ticks and failing results in 24 h,
what is broken now with owner and next step, what was fixed, and the rule hits per surface (how often each loop's
observers checked and caught something). Run it from cron once a day; its idempotent key means a second run the same
day edits the card instead of pinging again.

## Setup

```bash
cp system-agent/config.example.json system-agent/config.json     # declare YOUR checks
python3 system-agent/system_agent.py check --dry                  # see what it would do; nothing is executed
crontab -e
#   */10 * * * *  cd /path/to/repo && python3 system-agent/system_agent.py check
#   * * * * *     cd /path/to/repo && python3 system-agent/unstuck.py tick
#   0 8 * * *     cd /path/to/repo && python3 system-agent/system_agent.py update
python3 system-agent/test_registry.py && python3 system-agent/test_fix_guard.py
```

`touch data/system-agent/OFF` stops every check until the file is removed.
