"""hit_filter.py: the ONE test for "this rule-hits.jsonl line came from a test fixture".

rule-hits.jsonl is append-only and never edited. A test that writes fixture lines into the real file must not move a
skill compile, a rule stat or the daily update, so every reader that COUNTS hits (compile_skill.load_hits,
rule_stats.py, the system agent's daily update) skips them through is_test_hit().

A test item = any ':'-separated segment of `item` that starts with "zz-" (the fixture prefix for fake people and
artifacts) or is exactly "test". Stdlib only, no side effects on import.
"""
import re

TEST_SEGMENT = re.compile(r"^(zz-.*|test)$")


def is_test_hit(row) -> bool:
    """True when the hit line's item names a test fixture."""
    if not isinstance(row, dict):
        return False
    return any(TEST_SEGMENT.match(seg) for seg in str(row.get("item") or "").split(":"))


if __name__ == "__main__":
    assert is_test_hit({"item": "crm:zz-lane-claim:v1"})
    assert is_test_hit({"item": "crm:test:chat"})
    assert is_test_hit({"item": "zz-wait-due"})
    assert not is_test_hit({"item": "crm:healthcheck:1"})
    assert not is_test_hit({"item": "crm:person-123:v3"})
    assert not is_test_hit({"item": "trip:testa-rossa"})
    assert not is_test_hit({"item": None})
    assert not is_test_hit("x")
    print("hit_filter: 8 checks passed")
