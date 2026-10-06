# Security

## What this software can do

The SODA system is built to act for one person: it can send messages on their behalf, run commands on their machine
and read their inboxes through whatever pollers they connect. Its central safety property is that **nothing reaches
another person without an explicit yes from the owner**, given on a hub card. Keep it that way when you extend it:

- A producer never sends. It drafts and posts a card; the hub runs the action on yes.
- `$SODA_HOME/hub/executors.json` is the complete list of what a yes can run. Each entry is an argv run without a
  shell, with placeholders filled from the card's action. Keep it short, review every entry, and never add an executor
  that takes a free-form command.
- `system-agent/fix_guard.py` is a PreToolUse hook for any autonomous fix session: it refuses sends, sign-ins,
  deletions, destructive git, database deletes, permission changes and secret files. Use it whenever a model runs
  with tools unattended.

## Deployment

- Bind every service to `127.0.0.1` (the default for the hub and the brain). The hub and its review page have **no
  authentication**. Reach them from elsewhere only through an authenticated tunnel or a private network.
- The brain requires `X-Soda-Token` for every non-loopback request; a request forwarded by a local proxy counts as
  remote. Generate tokens with `openssl rand -hex 32`, keep them in `.env` (mode 600), never in the repository.
- `SODA_TOKEN_RO` is a read-only token (search and MCP only) for anything you expose more widely.
- Run exactly one Telegram poller per bot, and restrict it to your own chat id (`TELEGRAM_CHAT_ID`); the hub ignores
  every other chat.
- Keep the venv and the data directory outside any repository you sync or publish: state files contain message text.

## Data

This repository contains no personal data. All ledgers, hits, notes and cards under `examples/` are invented. The
`.gitignore` excludes `.env`, `data/` and every `*.jsonl` except the example files. Before publishing a fork, scan it
(for example `gitleaks detect` or `trufflehog filesystem .`).

## Reporting a vulnerability

Please open a private security advisory on the repository (GitHub: Security, "Report a vulnerability") rather than a
public issue. Include what you found, how to reproduce it, and the impact you expect.
