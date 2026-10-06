#!/usr/bin/env bash
# quickstart.sh: create the data directory from the examples and compile every example ledger into its skill.
# Idempotent: never overwrites a ledger or a skill map that already exists in $SODA_HOME (your rules are yours).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
PY="${PYTHON:-python3}"
# the same resolution every tool uses: environment first, then .env, then the default <repo>/data
SODA_HOME="$("$PY" -c 'import sys; sys.path.insert(0, "lib"); import soda_paths as P; print(P.HOME)')"
echo "data dir: $SODA_HOME"
mkdir -p "$SODA_HOME/ledgers" "$SODA_HOME/skill-maps" "$SODA_HOME/hub" "$SODA_HOME/feedback"

for f in examples/ledgers/*.example.md; do
  dst="$SODA_HOME/ledgers/$(basename "${f%.example.md}").md"
  [ -e "$dst" ] || { cp "$f" "$dst"; echo "ledger    $dst"; }
done
for f in examples/skill-maps/*.json; do
  dst="$SODA_HOME/skill-maps/$(basename "$f")"
  [ -e "$dst" ] || { cp "$f" "$dst"; echo "skill map $dst"; }
done
[ -e "$SODA_HOME/hub/hub-rules.json" ] || cp hub/hub-rules.example.json "$SODA_HOME/hub/hub-rules.json"
[ -e "$SODA_HOME/hub/executors.json" ] || cp hub/executors.example.json "$SODA_HOME/hub/executors.json"

for key in $("$PY" -c 'import json;print(" ".join(sorted(json.load(open("config/surfaces.json"))["ledgers"])))'); do
  if [ -e "$SODA_HOME/skill-maps/skill-map-$key.json" ]; then
    "$PY" rules/compile_skill.py --contract "$key" --install | tail -n 1
  fi
done
echo
echo "done. Next: node hub/server.js   (then see README, 'Post a test card')"
