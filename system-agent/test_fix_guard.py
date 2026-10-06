#!/usr/bin/env python3
"""Tests of the fix session's never-list (stdlib unittest): python3 system-agent/test_fix_guard.py"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_guard as G  # noqa: E402


class FixGuard(unittest.TestCase):
    def test_blocked(self):
        for cmd in ["rm -rf /tmp/x", "git reset --hard HEAD", "psql -c 'DROP TABLE x'", "chmod 777 f",
                    "cat .env", "pkill node", "claude -p 'fix it'", "curl https://api.telegram.org/botX/sendMessage",
                    "bash -c 'rm x'", "ssh host 'systemctl restart x'", "gcloud auth login"]:
            self.assertIsNotNone(G.check_shell(cmd), cmd)

    def test_allowed(self):
        for cmd in ["systemctl --user status soda-hub", "grep 'sendMessage' notif/push.js", "ls -la data",
                    "curl -s http://127.0.0.1:4180/pending", "ssh host 'journalctl -n 50 > /dev/null'", "mv a.js a.js.bak"]:
            self.assertIsNone(G.check_shell(cmd), cmd)

    def test_files_and_tools(self):
        self.assertIsNotNone(G.check({"tool_name": "Write", "tool_input": {"file_path": "/srv/app/.env"}}))
        self.assertIsNotNone(G.check({"tool_name": "Edit", "tool_input": {"file_path": "/x/settings.local.json"}}))
        self.assertIsNone(G.check({"tool_name": "Edit", "tool_input": {"file_path": "/srv/app/server.js"}}))
        self.assertIsNotNone(G.check({"tool_name": "mcp__mail__send_message", "tool_input": {}}))

    def test_readonly_paths(self):
        os.environ["SYSAGENT_READONLY_PATHS"] = "/srv/synced-repo"
        try:
            self.assertIsNotNone(G.check_shell("sed -i s/a/b/ /srv/synced-repo/x.md"))
            self.assertIsNotNone(G.check({"tool_name": "Write", "tool_input": {"file_path": "/srv/synced-repo/a.md"}}))
            self.assertIsNone(G.check_shell("cat /srv/synced-repo/x.md"))
        finally:
            del os.environ["SYSAGENT_READONLY_PATHS"]

    def test_agent_cap(self):
        with tempfile.TemporaryDirectory() as d:
            ev = {"tool_name": "Agent", "tool_input": {}}
            self.assertIsNone(G.check(ev, d))
            self.assertIsNone(G.check(ev, d))
            self.assertIsNotNone(G.check(ev, d))


if __name__ == "__main__":
    unittest.main()
