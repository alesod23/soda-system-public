"""llm.py: one way to ask the model for a judgement, used by every decider in this repo.

The SODA system runs on the Claude Code CLI in non-interactive mode (`claude -p`): no API key in code, no tools, no
MCP servers, no session persistence; the prompt goes in on stdin and the answer is plain text. Opus decides wherever
something is decided or written; a cheaper model at most classifies.

    SODA_LLM_BIN     the CLI (default: claude)
    SODA_LLM_MODEL   the model alias (default: opus)
    SODA_LLM_STUB    a file whose text answers every call (tests and offline demos: nothing leaves the machine)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths  # noqa: E402,F401  (loads .env)

TIMEOUT = int(os.environ.get("SODA_LLM_TIMEOUT", "240"))


def llm(prompt: str, model: str | None = None) -> str:
    """One answer as text."""
    stub = os.environ.get("SODA_LLM_STUB")
    if stub:
        return Path(stub).read_text(encoding="utf-8")
    # a child of a Claude Code session must not inherit the session's identity
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_SESSION"))}
    env["DISABLE_AUTOUPDATER"] = "1"
    cmd = [os.environ.get("SODA_LLM_BIN", "claude"), "-p", "--model", model or os.environ.get("SODA_LLM_MODEL", "opus"),
           "--strict-mcp-config", "--tools", "", "--no-session-persistence", "--output-format", "text"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=TIMEOUT, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
    return r.stdout


def parse_json(raw: str) -> dict:
    """The first {...} object in a model answer (models sometimes wrap JSON in prose or fences)."""
    t = str(raw or "")
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no JSON in the answer: " + t[-200:])
    return json.loads(t[a:b + 1])


def run_py(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run one of this repo's Python tools with the same interpreter; -> (exit code, stdout+stderr)."""
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")
