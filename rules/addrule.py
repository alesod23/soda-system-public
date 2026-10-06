#!/usr/bin/env python3
"""
addrule.py - turn one sentence of the owner's feedback into a rule, in one call.

Why it exists: the moment someone looks at a draft, a card or a CRM row and says "this is bad because X", that sentence
must become a rule the NEXT artifact is judged by, without anybody hand-editing a markdown table or picking the next
rule number by eye. One call, from any session, any machine; the rule is live on the next compile.

  addrule.py --contract email --rule "The ask goes first, subject included." \
             --quote "put the ask at the top, I don't want to scroll for it" \
             [--soft] [--source "owner ruling 2026-01-15"] [--item <artifact id>]

HARD by default (an observer may block or regenerate on it). --soft files it as a judgement flag instead.
Prints the number it assigned. Never renumbers what is already there.

"I like this" is filed the same way, as a POSITIVE entry:

  addrule.py --contract email --like --quote "the sentence they liked" --item <draft id> [--maps-to H18] [--rule "why"]

It appends a row whose text starts with "LIKED:" and carries the quote and the artifact id. compile_skill.py turns it
into the example of the rule it satisfies when --maps-to names one, otherwise lists it under "Reference examples".
--item works on any rule: it is the artifact id the sentence was given on (a draft id, a hub card id, a person id).

Which ledgers exist is config/surfaces.json; where they live is $SODA_HOME/ledgers (lib/soda_paths.py).
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths as P  # noqa: E402

# The hub's live filter: a pattern filed with --suppress is applied by the hub on the next card it receives.
HUB_RULES_FILE = P.HOME / "hub" / "hub-rules.json"
# Each ledger names its SOFT section slightly differently; match the header, not the title.
SOFT_RE = re.compile(r"^## SOFT\b.*$", re.M)


def main():
    keys = P.ledger_keys()
    p = argparse.ArgumentParser(description="file one sentence of feedback as a ledger row")
    p.add_argument("--rule", default="", help="The rule, in one or two sentences (optional with --like).")
    p.add_argument("--quote", default="", help="The owner's exact words, so the rule can be repaired at the source. With --like: the sentence they liked.")
    p.add_argument("--source", default="", help="Override the source cell (default: owner ruling <today>).")
    p.add_argument("--soft", action="store_true", help="File as a SOFT flag instead of a HARD rule.")
    p.add_argument("--item", default="", help="The artifact id the feedback was given on (draft id, card id, person id).")
    p.add_argument("--like", action="store_true",
                   help='"I like this": file a positive LIKED entry (needs --quote; --item recommended).')
    p.add_argument("--maps-to", default="", metavar="H<n>",
                   help="With --like: the existing rule this liked sentence satisfies; the compile uses it as that rule's example.")
    p.add_argument("--suppress", default="", metavar="REGEX",
                   help="hub only: ALSO file this regex into $SODA_HOME/hub/hub-rules.json so the hub's guard "
                        "downgrades a matching card to kind 'update' from now on. --drop-instead silences it entirely.")
    p.add_argument("--drop-instead", action="store_true",
                   help="With --suppress: file under 'drop' (pushes nothing) instead of 'downgrade'.")
    p.add_argument("--contract", default=P.surfaces().get("default_ledger", keys[0]), choices=keys,
                   help="Which ledger the rule belongs to (config/surfaces.json).")
    p.add_argument("--ledger-file", help=argparse.SUPPRESS)   # tests: write another copy of the ledger
    a = p.parse_args()

    if not a.like and not a.rule.strip():
        p.error("--rule is required (unless --like)")
    if a.like and not a.quote.strip():
        p.error("--like needs --quote (the sentence they liked)")
    if a.like and a.soft:
        p.error("--like and --soft do not combine: a like is neither HARD nor SOFT")
    if a.maps_to and not a.like:
        p.error("--maps-to only makes sense with --like")

    contract = Path(a.ledger_file) if a.ledger_file else P.ledger_path(a.contract)
    if not contract.exists():
        sys.exit(f"ledger not found: {contract} (run scripts/quickstart.sh, or copy one from examples/ledgers)")
    s = contract.read_text(encoding="utf-8")

    # Next free H-number. Read them all; never assume the last line is the highest.
    nums = [int(m) for m in re.findall(r"^\| H(\d+) \|", s, flags=re.M)]
    n = (max(nums) + 1) if nums else 1

    if a.maps_to:
        m2 = re.fullmatch(r"H(\d+)", a.maps_to.strip())
        if not m2 or int(m2.group(1)) not in nums:
            sys.exit(f"--maps-to must name an existing rule (H1..H{max(nums) if nums else 0}), got {a.maps_to!r}")
        a.maps_to = f"H{int(m2.group(1))}"

    # Table cells cannot carry a pipe; the quote is kept verbatim otherwise.
    clean = lambda t: t.strip().replace("|", "/").replace("\n", " ")
    today = dt.date.today().isoformat()
    if a.like:
        source = a.source or f"owner liked it {today}"
        body = "LIKED: " + (clean(a.rule).rstrip(".") + "." if a.rule.strip() else "liked this as written.")
        body += f' *"{clean(a.quote)}"*'
        if a.item.strip():
            body += f" [item: {clean(a.item)}]"
        if a.maps_to:
            body += f" [maps-to: {a.maps_to}]"
    else:
        source = a.source or f"owner ruling {today}"
        body = clean(a.rule).rstrip(".") + "."
        if a.quote.strip():
            body += f' *"{clean(a.quote)}"*'
        if a.item.strip():
            body += f" [item: {clean(a.item)}]"
        if a.soft:
            body = "**SOFT, flag only.** " + body

    row = f"| H{n} | {body} | {clean(source)} |\n"

    # New rows go at the end of the HARD tables, i.e. just above the SOFT section header.
    m = SOFT_RE.search(s)
    if not m:
        sys.exit(f"no '## SOFT' section in {contract.name}; layout changed, add by hand")
    s = s[:m.start()] + row + "\n" + s[m.start():]
    contract.write_text(s, encoding="utf-8")
    print(f"H{n} added ({'liked' if a.like else 'soft' if a.soft else 'hard'}) -> {contract}")
    print(f"  {body[:160]}")

    # --suppress closes the loop for the hub: the sentence becomes a written rule AND a live filter in the same turn.
    # Without it the rule is a note nobody reads at POST time.
    if a.suppress:
        if a.contract != "hub":
            sys.exit("--suppress only applies to --contract hub")
        try:
            re.compile(a.suppress)
        except re.error as e:
            sys.exit(f"--suppress is not a valid regex: {e}")
        hr = HUB_RULES_FILE
        try:
            data = json.loads(hr.read_text(encoding="utf-8")) if hr.exists() else {"downgrade": [], "drop": []}
        except ValueError as e:
            sys.exit(f"cannot read {hr}: {e}")
        bucket = "drop" if a.drop_instead else "downgrade"
        data.setdefault(bucket, []).append({"match": a.suppress, "rule": f"H{n}", "note": a.rule.strip()[:120]})
        hr.parent.mkdir(parents=True, exist_ok=True)
        hr.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  pattern filed under '{bucket}' in {hr}; the hub applies it on the next card")


if __name__ == "__main__":
    main()
