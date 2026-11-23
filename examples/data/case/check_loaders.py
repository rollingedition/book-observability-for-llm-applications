#!/usr/bin/env python3
"""Check the case loaders (examples/lib/case.py) and print a short summary.

The summary must be the same under PYTHONHASHSEED=0 and PYTHONHASHSEED=1 (plan section 1.4, rule 11):

    PYTHONHASHSEED=0 python examples/data/case/check_loaders.py > a.txt
    PYTHONHASHSEED=1 python examples/data/case/check_loaders.py > b.txt
    cmp a.txt b.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the examples folder, so that `lib` imports

from lib import SEED  # noqa: E402,F401  (first: threads and seeds are fixed before the loaders load)
from lib import case  # noqa: E402


def main() -> None:
    print(f"seed {SEED}")
    for day in ["2026-12-07", "2026-12-08", "2026-12-08-paired", "2026-12-09", "2026-12-10"]:
        msgs = case.load_messages(day)
        first = msgs[0].arrival_ns if msgs else None
        last = msgs[-1].arrival_ns if msgs else None
        print(f"{day}: {len(msgs)} messages, first {first} ns, last {last} ns")
    users = case.load_users()
    print(f"users: {len(users)}, first {users[0].user_id}, last {users[-1].user_id}")
    arts = case.load_articles()
    print(f"articles: {len(arts)}, first {arts[0].article_id}, last {arts[-1].article_id}")
    print("routes: " + ", ".join(f"{r.name}({r.tool or '-'}, retrieves={r.retrieves})" for r in case.load_routes()))
    scen = sorted({m.scenario for m in case.load_messages("2026-12-08")})
    print("routes of the baseline scenarios: " + ", ".join(f"{s}->{case.scenario_to_route(s)}" for s in scen))
    base = [m.message_id for m in case.load_messages("2026-12-08")]
    for loc in ["de-DE", "ja-JP", "hi-IN", "ar-SA", "zh-CN"]:
        texts = case.load_locale(loc, base)
        print(f"{loc}: {len(texts)} texts, first {len(texts[0].encode('utf-8'))} bytes")
    print("policies: " + ", ".join(case.load_policies()))
    for kind in ["calendar", "alarms", "lists", "contacts"]:
        print(f"records {kind}: {len(case.load_records(kind))}")
    print("tool durations: " + ", ".join(f"{k}={v}" for k, v in case.load_tool_durations().items()))


if __name__ == "__main__":
    main()
