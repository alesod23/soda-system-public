#!/usr/bin/env python3
"""fix_guard.py: the PreToolUse hook of a fix session (docs/05-system-agent.md, "the never-list, enforced by a hook,
not by trust"). Wire it into the session's settings as a PreToolUse command hook.

Reads the hook JSON on stdin ({tool_name, tool_input, ...}); exit 0 = allowed; exit 2 with the reason on stderr =
blocked (the session sees the reason and must answer `status: stuck` with a for_owner action instead).

The never-list: never sign in; never send anything to a person; never delete data (it may disable, rename to .bak and
stop); never change an env file, a credential or a permission; never write into a read-only path (env
SYSAGENT_READONLY_PATHS, colon-separated: e.g. git-synced repos that a sync job owns); never open a window; never kill
a process; never start a nested model session; at most 2 sub-agents per session.

    fix_guard.py --test "<bash command>"     prints allowed / blocked: <why>
"""
import json
import os
import re
import sys
from pathlib import Path

SHELL_RULES = [
    (r"\b(auth\s+login|reauth|oauth|--login-hint|gcloud\s+auth|rclone\s+(authorize|config))\b", "a sign-in"),
    (r"(\bsend(mail|_message|Message|-draft)?\b|\bsmtp\b|api\.telegram\.org|chat\.postMessage|/messages\b)", "a send to a person"),
    (r"(^|[\s;&|('\"])(rm|rmdir|del|erase|shred|unlink)\s", "a deletion (rename to .bak instead)"),
    (r"\bRemove-Item\b|\bClear-Content\b", "a deletion (rename to .bak instead)"),
    (r"\bgit\s+(clean|reset\s+--hard|checkout\s+(--\s+)?\.(\s|$)|push\s+(-f|--force)|branch\s+-D)", "a destructive git command"),
    (r"\bschtasks(\.exe)?\s+/delete\b|\bUnregister-ScheduledTask\b|\bsystemctl\s+(--user\s+)?(disable\s+--now|mask)\b", "removing a scheduled job (disable it instead)"),
    (r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b|\bDELETE\s+FROM\b", "deleting database rows"),
    (r"\b(chmod|chown|icacls|takeown|Set-Acl|sudo)\b", "a permission change"),
    (r"(^|[\s'\"/\\])\.env(\.[a-z]+)?(\s|$|['\"])|[/\\]\.env[/\\]", "an env file"),
    (r"settings(\.local)?\.json|credentials\.json|allowedTools|permissions\.allow", "a permission or settings file"),
    (r"\b(Start-Process|Invoke-Item|explorer(\.exe)?|xdg-open|open\s+-a)\b", "opening a window"),
    (r"\b(Stop-Process|taskkill|pkill|killall)\b|(^|[\s;&|])kill\s+-?\d", "killing a process"),
    (r"\bclaude(\.exe)?\s+(-p|--print)\b", "a nested model session"),
]
SSH = re.compile(r"\b(ssh|scp|rsync)\b", re.I)
REMOTE_WRITE = re.compile(r"(>|>>|\btee\b|\bmv\b|\bcp\b|\bsed\s+-i|\bsystemctl\s+(--user\s+)?(start|stop|restart|enable|disable|daemon-reload)|"
                          r"\bgit\s+(commit|checkout|reset|pull|push|merge|stash|apply|am|rebase)|\bcrontab\s+-[er]|\btouch\b|\bmkdir\b|"
                          r"\bnpm\s+(i|install)|\bpip3?\s+install|\brm\b)", re.I)
SECRET_PATH = re.compile(r"(^|[\\/])(\.env([\\/.]|$)|tokens?[\\/]|credentials\.json|settings(\.local)?\.json|\.session$)", re.I)
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
HARMLESS_REDIR = re.compile(r"\d?>\s*/dev/null|\d?>&\d", re.I)


def readonly_paths():
    return [p for p in (os.environ.get("SYSAGENT_READONLY_PATHS") or "").split(":") if p.strip()]


def check_shell(cmd):
    c = cmd or ""
    m = SSH.search(c)
    if m and REMOTE_WRITE.search(HARMLESS_REDIR.sub(" ", c[m.start():])):
        return "a write on another machine over ssh: remote hosts are read-only for a fix session"
    bare = QUOTED.sub(" ", c)          # a pattern inside quotes (grep "sendMessage") is text, not an action
    if re.search(r"\b(bash|sh|zsh|powershell|pwsh|cmd|python3?|psql|sqlite3|mysql)(\.exe)?\s+(\S+\s+)*?(-c|-e|-Command|/c)\b", c, re.I):
        bare = c                        # ... unless the quotes ARE the command (bash -c 'rm x')
    for pat, why in SHELL_RULES:
        if re.search(pat, bare, re.I):
            return why
    for ro in readonly_paths():
        if ro in c and re.search(r"(>|\btee\b|\bmv\b|\bcp\b|\bsed\s+-i|\bgit\s+(commit|checkout|reset|merge|stash|rebase))", c):
            return f"a write into a read-only path ({ro}): its sync job owns it"
    return None


def check(event, state_dir=None):
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input") or {}
    if tool in ("Bash", "PowerShell"):
        return check_shell(str(ti.get("command") or ""))
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if SECRET_PATH.search(p):
            return "an env, token, credential or settings file"
        for ro in readonly_paths():
            if p.startswith(ro):
                return f"a file in a read-only path ({ro})"
        return None
    if tool == "Agent":
        f = Path(state_dir or os.environ.get("FIX_JOB_DIR") or ".") / "agents.count"
        try:
            n = int(f.read_text() or 0)
        except (OSError, ValueError):
            n = 0
        if n >= 2:
            return "more than 2 sub-agents in one fix session"
        try:
            f.write_text(str(n + 1))
        except OSError:
            pass
        return None
    if tool.startswith("mcp__") and re.search(r"send|reply|create_draft|forward|connect|post|message|trash|delete", tool, re.I):
        return "an MCP tool that sends or deletes"
    return None


def main():
    if "--test" in sys.argv:
        why = check_shell(sys.argv[sys.argv.index("--test") + 1])
        print("allowed" if not why else "blocked: " + why)
        return 0
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    why = check(ev)
    if why:
        sys.stderr.write(f"BLOCKED by the fix session's never-list: {why}. If the fix needs this, stop and answer "
                         "status stuck with a for_owner action.\n")
        try:
            with open(Path(os.environ.get("FIX_JOB_DIR") or ".") / "guard.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"tool": ev.get("tool_name"), "input": str(ev.get("tool_input"))[:400], "why": why}) + "\n")
        except OSError:
            pass
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
