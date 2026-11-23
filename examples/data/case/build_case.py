#!/usr/bin/env python3
"""Build the Ambrewick case's data (plan section 2, PL-28 to PL-43).

Run from the book's folder:

    python examples/data/case/build_case.py --massive-tar PATH --massive-repo PATH --slurp-license PATH [--out DIR]

The three PATH arguments name files outside this book: the MASSIVE 1.1 tarball, the MASSIVE repository checkout at
the pinned commit, and the SLURP licence text. The script reads them in place and never extracts them. Everything the
script writes is a function of those inputs and the seeds below; running it twice gives the same SHA256SUMS.

Written by hand, not by this script: names.toml, traffic.toml, tool_durations.toml, prices.toml, routing_examples.toml,
policies.md and articles/*.md. The script reads the first four, and checks the last three and the articles' count,
length and content (check_authored).

Uses only the standard library and numpy (pinned in examples/requirements.txt). Prints ASCII only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import sys
import tarfile
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from numpy.random import PCG64, Generator

HERE = Path(__file__).resolve().parent

MASSIVE_SHA256 = "4cba5faa11c71437928e17cb1b9b3d8b8e727e7ea363a3a9a8045e19c0491577"
MASSIVE_BYTES = 40251390
MASSIVE_URL = "https://amazon-massive-nlu-dataset.s3.amazonaws.com/amazon-massive-dataset-1.1.tar.gz"
MASSIVE_LAST_MODIFIED = "Mon, 07 Nov 2022 16:55:04 GMT"
MASSIVE_REPO = "https://github.com/alexa/massive"
MASSIVE_COMMIT = "f966f21846043aabef9b0f974fa7970027f43738"
SLURP_SHA256 = "97547dd6236684935697b26629fb6e70b3be4ed535c2d85fdc053a6389192849"

LOCALES = ["de-DE", "ja-JP", "hi-IN", "ar-SA", "zh-CN"]  # the cost chapter's locales (PL-20, section 11)
BASE_SEED = 20261122
PAIRED_SEED = 20261208  # section 2.8: Generator(PCG64(20261208)) for the paired subset
STAGE = {"windows": 1, "incident_background": 2, "dev": 3, "baseline1": 4, "baseline2": 5,
         "users": 6, "contacts": 7, "calendar": 8, "alarms": 9, "lists": 10,
         "window_replacement": 11, "window_users": 12, "window_fixed": 13}  # added last: no earlier stage's draw changes
PERSON_SLOT = re.compile(r"\[person\s*:")  # MASSIVE's annotation for a person name in an utterance

DAYS = {  # section 2.8, UTC; the key is the traffic file's name
    "2026-12-07": "dev",
    "2026-12-08": "baseline1",
    "2026-12-08-paired": "paired",
    "2026-12-09": "incident",
    "2026-12-10": "baseline2",
}
HOUR_US = 3_600_000_000  # microseconds per hour (arrivals are whole microseconds, so the ISO text is exact)
DAY_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Section 2.3: each scenario goes to exactly one route. The check is on the scenario label.
SCENARIO_ROUTES = {
    "alarm": "alarms", "audio": "devices", "calendar": "calendar", "cooking": "fallback",
    "datetime": "fallback", "email": "messages", "general": "fallback", "iot": "devices", "lists": "lists",
    "music": "media", "news": "fallback", "play": "media", "qa": "fallback", "recommendation": "fallback",
    "social": "messages", "takeaway": "fallback", "transport": "fallback", "weather": "fallback",
}
ROUTE_TOOLS = {  # route -> (tool, retrieves)
    "alarms": ("list_alarms", False), "calendar": ("list_events", False), "devices": (None, True),
    "fallback": (None, False), "lists": ("list_items", False), "media": (None, True),
    "messages": ("find_contact", True),
}

# Section 2.8 and 2.9: the three injected windows (UTC, 2026-12-09). Window messages are placed by the generator.
WINDOWS = [
    {"id": "inc-contacts-timeout", "start": 13 * 3600, "end": 13 * 3600 + 12 * 60, "pool": "email_contact",
     "n": 12, "kind": "contacts-timeout",
     "fault": "find_contact calls time out at 2.0 s; the tool wrapper retries once after 0.5 s"},
    {"id": "inc-rate-limit", "start": 15 * 3600 + 30 * 60, "end": 15 * 3600 + 31 * 60 + 30, "pool": "model_route",
     "n": 12, "kind": "rate-limit",
     "fault": "the model endpoint answers 429 to every request in the window; the client retries with backoff"},
    {"id": "inc-mcp-error", "start": 17 * 3600, "end": 17 * 3600 + 10 * 60, "pool": "email_contact",
     "n": 12, "kind": "mcp-error",
     "fault": "the contacts server returns an error result for find_contact"},
]
EMAIL_CONTACT_INTENTS = {"email_querycontact", "email_addcontact"}

# Managing editor's decision (2026-11-23, revised). Three utterances already known to reach find_contact on the 4B model
# (the baseline day 1 calls 16297, 16401 and 17052) join the timeout window in place of its three lowest-numbered
# messages that routed to fallback in the current incident-day recording (15963, 15994, 16038). Each keeps its MASSIVE
# ID; each is on 2026-12-09 once, and repeating it on another day is ordinary traffic. The timeout window's hit rate is
# therefore chosen by these utterances, not a natural rate (chapter 8 says so).
TIMEOUT_DROP = ["15963", "15994", "16038"]
TIMEOUT_ADD = ["16297", "16401", "17052"]
REPEATED_ON_INCIDENT = set(TIMEOUT_ADD)  # on 2026-12-08 as well; exempt from the cross-day check only

GIVEN = [
    "Aldren", "Bexa", "Corrin", "Dalia", "Ellery", "Fenna", "Gorrin", "Hesper", "Ilse", "Jovan", "Kasimir",
    "Linnea", "Marrow", "Nerys", "Oskar", "Pelle", "Quillan", "Rasha", "Sigrun", "Tavin", "Ulric", "Vessa",
    "Wenna", "Xavel", "Ysolde", "Zeyna", "Anwen", "Brannoc", "Caelan", "Dervla", "Eilis", "Fabel", "Ganzo",
    "Halvet", "Imrie", "Jessamy", "Kiva", "Lorcan", "Mireille", "Oriel",
]
SURNAME = [
    "Varrow", "Kestrell", "Odrun", "Pennick", "Lathwell", "Marrane", "Quennel", "Hasslow", "Tennery", "Oakhollow",
    "Gillaway", "Dunmark", "Ashvale", "Trelane", "Benmore", "Sennick", "Harrowby", "Renfrow", "Mirelle", "Corvan",
    "Pellam", "Yeardley", "Fosset", "Galloran", "Wickery", "Helmsworth", "Jorrell", "Kinnaird", "Lowmere",
    "Stavery", "Peverell", "Quarrick", "Moravell", "Thistlery", "Ormsby", "Vauncey", "Brackwell", "Ysmere",
    "Duffield", "Cairnley",
]
CALENDAR_TITLES = [
    "Team check-in", "Dentist", "Lunch with a friend", "Gym session", "Project review", "School pickup",
    "Book club", "Planning session", "Coffee with a neighbour", "Car service", "Client call",
    "Doctor follow-up", "Yoga class", "Quarterly review", "Music lesson", "Volunteer shift", "Office move",
    "Pharmacy pickup", "Family dinner", "Training run",
]
ALARM_LABELS = [
    "Wake up", "Gym", "Medication", "Commute", "Bins out", "Tea", "Weekend lie-in", "Call home", "Oven",
    "Study break",
]
LIST_ITEMS = {
    "packing": ["charger", "umbrella", "passport", "sunscreen", "socks", "raincoat", "toothbrush", "adapter",
                "notebook", "water bottle"],
    "shopping": ["milk", "bread", "eggs", "apples", "coffee", "rice", "pasta", "tomatoes", "butter", "lemons",
                 "yoghurt", "spinach"],
    "to do": ["renew library books", "book the dentist", "call the bank", "water the plants",
              "file the receipts", "order printer ink", "check the tyre pressure", "pay the parking fine"],
}

SLOT_RE = re.compile(r"\[([a-z_]+) : ([^\]]*)\]")


# ----------------------------------------------------------------------------- helpers

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm(s: str) -> str:
    """Case-fold, drop full stops and apostrophes, collapse spaces (the names rule's matching form)."""
    s = s.lower().replace(".", "").replace("'", "").replace("\u2019", "")
    return " ".join(s.split())


def term_regex(terms) -> re.Pattern:
    pats = sorted({norm(t) for t in terms}, key=len, reverse=True)
    body = "|".join(r"\s+".join(re.escape(w) for w in p.split()) for p in pats)
    return re.compile(r"(?<![a-z0-9])(" + body + r")(?![a-z0-9])")


def iso_at(day: str, offset_us: int) -> str:
    base = dt.datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(microseconds=offset_us)).strftime(DAY_TIME_FMT)


def rng_for(stage: str) -> Generator:
    seed = PAIRED_SEED if stage == "paired" else BASE_SEED + STAGE[stage]
    return Generator(PCG64(seed))


def pick(rng: Generator, cands: list, n: int) -> list:
    """n distinct items, returned in the candidates' own order, so the result never depends on draw order."""
    if n > len(cands):
        raise SystemExit(f"need {n} items, have {len(cands)}")
    idx = rng.choice(len(cands), size=n, replace=False)
    return [cands[i] for i in sorted(int(i) for i in idx)]


def arrivals_us(rng: Generator, n: int, weights: list[int]) -> list[int]:
    """Arrival offsets from midnight UTC, in microseconds. Given the count, a Poisson process's arrivals are i.i.d.
    draws from its normalised rate, so each message takes an hour by the weight table and a uniform offset within it."""
    p = np.array(weights, dtype=float)
    p = p / p.sum()
    hours = rng.choice(len(weights), size=n, p=p)
    offs = rng.integers(0, HOUR_US, size=n)
    return [int(h) * HOUR_US + int(o) for h, o in zip(hours, offs)]


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(s: str) -> bool:
    moved = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in moved)
    return int(num) % 97 == 1


# ----------------------------------------------------------------------------- inputs

def read_massive(tar_path: Path) -> dict:
    data = tar_path.read_bytes()
    if len(data) != MASSIVE_BYTES or sha256_bytes(data) != MASSIVE_SHA256:
        raise SystemExit("MASSIVE tarball does not match the pinned size and SHA-256 (massive.md, M-1)")
    out = {"locales": {}}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        def read(name: str) -> bytes:
            return t.extractfile(name).read()
        out["en"] = [json.loads(line) for line in read("1.1/data/en-US.jsonl").decode("utf-8").splitlines()
                     if line.strip()]
        for loc in LOCALES:
            out["locales"][loc] = [json.loads(line) for line in read(f"1.1/data/{loc}.jsonl").decode("utf-8")
                                   .splitlines() if line.strip()]
        out["LICENSE"] = read("1.1/LICENSE")
        out["NOTICE"] = read("1.1/NOTICE.md")
    return out


def load_config() -> tuple[dict, dict, dict, dict]:
    names = tomllib.loads((HERE / "names.toml").read_text(encoding="utf-8"))
    traffic = tomllib.loads((HERE / "traffic.toml").read_text(encoding="utf-8"))
    tools = tomllib.loads((HERE / "tool_durations.toml").read_text(encoding="utf-8"))
    prices = tomllib.loads((HERE / "prices.toml").read_text(encoding="utf-8"))
    for name in ["routing_examples.toml", "policies.md"]:
        if not (HERE / name).exists():
            raise SystemExit(f"missing authored file: {name}")
    return names, traffic, tools, prices


# ----------------------------------------------------------------------------- the names rule

def names_rule(recs: list[dict], names: dict) -> tuple[dict, dict]:
    """Return (removed, hits): removed maps id -> the first clause that matched; hits maps id -> every clause."""
    text_rx = term_regex(names["text_terms"] + names.get("scan_text_terms", []))
    brand_rx = term_regex(names["brand_slot_terms"])
    dot_rx = term_regex(names["url_phrases"])
    removed, hits = {}, {}
    for r in recs:
        slots = SLOT_RE.findall(r["annot_utt"])
        clauses = []
        if any(t in names["name_slot_types"] for t, _ in slots):
            clauses.append("a_name_slot")
        if text_rx.search(norm(r["utt"])):
            clauses.append("b_text")
        if any(t in names["brand_slot_types"] and brand_rx.search(norm(v)) for t, v in slots):
            clauses.append("c_brand_slot")
        if r["id"] in names["url_ids"]:
            clauses.append("m6_url_id")
        if dot_rx.search(norm(r["utt"])):
            clauses.append("m6_url_phrase")
        if clauses:
            hits[r["id"]] = clauses
            removed[r["id"]] = clauses[0]
    return removed, hits


def dedupe(recs: list[dict]) -> tuple[list[dict], int]:
    seen, out, dups = set(), [], 0
    for r in sorted(recs, key=lambda x: int(x["id"])):
        key = " ".join(r["utt"].lower().split())
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dups


# ----------------------------------------------------------------------------- generation

def build_users() -> list[dict]:
    rng = rng_for("users")
    users = []
    for n in range(1, 201):
        g = GIVEN[int(rng.integers(len(GIVEN)))]
        s = SURNAME[int(rng.integers(len(SURNAME)))]
        users.append({"user_id": f"U{n:04d}", "given": g, "surname": s,
                      "email": f"{g.lower()}.{s.lower()}{n % 100:02d}@users.example"})
    emails = [u["email"] for u in users]
    if len(set(emails)) != len(emails):
        raise SystemExit("user emails are not unique; change the local-part rule")
    return users


def build_contacts() -> list[dict]:
    rng = rng_for("contacts")
    combos = [(g, s) for g in GIVEN for s in SURNAME]
    out = []
    for n in range(1, 201):
        uid = f"U{n:04d}"
        k = int(rng.integers(8, 13))
        for g, s in pick(rng, combos, k):
            out.append({"user_id": uid, "name": f"{g} {s}", "email": f"{g.lower()}.{s.lower()}@contacts.example"})
    return out


def build_tool_records(users: list[dict]) -> dict:
    cal, alarms, lists = [], [], []
    rc, ra, rl = rng_for("calendar"), rng_for("alarms"), rng_for("lists")
    for u in users:
        uid = u["user_id"]
        for day in DAYS:
            if DAYS[day] == "paired":
                continue
            k = int(rc.integers(0, 6))
            minutes = pick(rc, list(range(0, 24 * 60)), k)
            for m in minutes:
                title = CALENDAR_TITLES[int(rc.integers(len(CALENDAR_TITLES)))]
                cal.append({"user_id": uid, "date": day, "time": f"{m // 60:02d}:{m % 60:02d}", "title": title})
        k = int(ra.integers(0, 6))
        for m in pick(ra, list(range(0, 24 * 60)), k):
            alarms.append({"user_id": uid, "time": f"{m // 60:02d}:{m % 60:02d}",
                           "label": ALARM_LABELS[int(ra.integers(len(ALARM_LABELS)))],
                           "enabled": bool(ra.random() < 0.8)})
        for name in sorted(LIST_ITEMS):
            k = int(rl.integers(0, 11))
            items = pick(rl, LIST_ITEMS[name], min(k, len(LIST_ITEMS[name])))
            lists.append({"user_id": uid, "list": name, "items": sorted(items)})
    # The IDs the tools act on (section 2.6): numbered in generation order and drawn from no seed, so the other
    # records and the traffic do not change.
    for n, row in enumerate(cal, 1):
        row["event_id"] = f"E{n:05d}"
    for n, row in enumerate(alarms, 1):
        row["alarm_id"] = f"A{n:05d}"
    return {"calendar": cal, "alarms": alarms, "lists": lists}


def draw_schedules(test: list[dict], train: list[dict], en_by_id: dict, traffic: dict) -> dict:
    """Section 2.8 and 2.9: the window messages first, then the rest, each day disjoint from the others."""
    used: set[str] = set()
    days: dict[str, list[dict]] = {}
    weights = traffic["arrivals"]["hourly_weights"]
    window_info = []

    email_contact = [r for r in test if r["scenario"] == "email" and r["intent"] in EMAIL_CONTACT_INTENTS]
    model_route = [r for r in test if SCENARIO_ROUTES[r["scenario"]] != "fallback"]
    if len(email_contact) < 24:
        raise SystemExit(f"only {len(email_contact)} email contact utterances; windows 1 and 3 need 24")

    rw = rng_for("windows")
    ec_pick = pick(rw, email_contact, 24)
    used.update(r["id"] for r in ec_pick)
    model_pick = pick(rw, [r for r in model_route if r["id"] not in used], 12)
    used.update(r["id"] for r in model_pick)
    picks = {"inc-contacts-timeout": ec_pick[:12], "inc-rate-limit": model_pick, "inc-mcp-error": ec_pick[12:]}

    incident_msgs = []
    for w in WINDOWS:
        arr = [w["start"] * 1_000_000 + int(x) for x in rw.integers(0, (w["end"] - w["start"]) * 1_000_000, size=w["n"])]
        for r, off in zip(sorted(picks[w["id"]], key=lambda x: int(x["id"])), arr):
            incident_msgs.append((r, off, w["id"]))
        window_info.append(w)

    rb = rng_for("incident_background")
    bg = pick(rb, [r for r in test if r["id"] not in used], 24)
    used.update(r["id"] for r in bg)
    for r, off in zip(bg, arrivals_us(rb, len(bg), weights)):
        incident_msgs.append((r, off, None))

    rd = rng_for("dev")
    dev = []
    for route in sorted(set(SCENARIO_ROUTES.values())):
        cands = [r for r in train if SCENARIO_ROUTES[r["scenario"]] == route and r["id"] not in {x["id"] for x in dev}]
        dev.append(pick(rd, cands, 1)[0])
    rest = [r for r in train if r["id"] not in {x["id"] for x in dev}]
    dev += pick(rd, rest, 20 - len(dev))
    dev_offs = arrivals_us(rd, len(dev), weights)
    days["2026-12-07"] = list(zip(sorted(dev, key=lambda x: int(x["id"])), dev_offs))

    rb1 = rng_for("baseline1")
    base1 = pick(rb1, [r for r in test if r["id"] not in used], 200)
    used.update(r["id"] for r in base1)
    base1_offs = arrivals_us(rb1, len(base1), weights)
    days["2026-12-08"] = list(zip(base1, base1_offs))

    paired_ids = sorted(pick(rng_for("paired"), sorted(base1, key=lambda x: int(x["id"])), 60),
                        key=lambda x: int(x["id"]))
    off_of = {r["id"]: o for r, o in days["2026-12-08"]}
    days["2026-12-08-paired"] = [(r, off_of[r["id"]]) for r in paired_ids]

    rb2 = rng_for("baseline2")
    base2 = pick(rb2, [r for r in test if r["id"] not in used], 80)
    used.update(r["id"] for r in base2)
    days["2026-12-10"] = list(zip(base2, arrivals_us(rb2, len(base2), weights)))

    days["2026-12-09"] = [(r, off, wid) for r, off, wid in incident_msgs]
    # The draw as it was before the replacement below. assign_users walks it, so the users stream is unchanged.
    user_draw_days = {"2026-12-09": list(days["2026-12-09"])}

    # Replacement of the two contacts windows (managing editor, 2026-11-23). Drawn last, from the test utterances no day
    # uses: email or social utterances that name a person (MASSIVE's person slot), so the 4B model is more likely to
    # call find_contact. Each slot keeps its arrival offset. The rate-limit window and the background stay as drawn.
    rr = rng_for("window_replacement")
    named = [r for r in test if r["id"] not in used and r["scenario"] in ("email", "social")
             and PERSON_SLOT.search(r["annot_utt"])]
    if len(named) < 24:
        raise SystemExit(f"only {len(named)} unused named email or social utterances; the windows need 24")
    new_ec = pick(rr, named, 24)
    used.update(r["id"] for r in new_ec)
    replaced = []
    for wid, chunk in (("inc-contacts-timeout", new_ec[:12]), ("inc-mcp-error", new_ec[12:])):
        slots = [off for _r, off, w in incident_msgs if w == wid]  # the draw's arrival offsets, in draw order
        replaced += [(r, off, wid) for r, off in zip(sorted(chunk, key=lambda x: int(x["id"])), slots)]
    # The revised decision (managing editor, 2026-11-23): three utterances already known to reach find_contact (the
    # baseline day 1 calls) take the places of the timeout window's three lowest-numbered messages that routed to
    # fallback in the current incident-day recording. The window keeps 12 messages. The three new messages get arrival
    # times drawn from the window's span with a stream of their own; the other nine keep their slots.
    # assign_users walks this list for the replacement's new users, so the three dropped messages keep their draws.
    user_walk = [(r, off, wid) for r, off, wid in days["2026-12-09"]
                 if wid not in ("inc-contacts-timeout", "inc-mcp-error")] + replaced
    dropped = [r["id"] for r, _o, w in replaced if w == "inc-contacts-timeout" and r["id"] in TIMEOUT_DROP]
    if sorted(dropped) != sorted(TIMEOUT_DROP):
        raise SystemExit(f"the timeout window does not hold the messages to drop: {TIMEOUT_DROP}")
    test_by_id = {r["id"]: r for r in test}
    add = [test_by_id[i] for i in TIMEOUT_ADD]
    if any(r["scenario"] != "email" or SCENARIO_ROUTES[r["scenario"]] != "messages" for r in add):
        raise SystemExit("a fixed timeout-window utterance does not reach find_contact")
    tw = next(w for w in WINDOWS if w["id"] == "inc-contacts-timeout")
    rf = rng_for("window_fixed")
    add_offs = sorted(tw["start"] * 1_000_000 + int(x)
                      for x in rf.integers(0, (tw["end"] - tw["start"]) * 1_000_000, size=len(add)))
    replaced = [(r, off, w) for r, off, w in replaced if not (w == "inc-contacts-timeout" and r["id"] in TIMEOUT_DROP)]
    replaced += [(r, off, "inc-contacts-timeout")
                 for r, off in zip(sorted(add, key=lambda x: int(x["id"])), add_offs)]
    days["2026-12-09"] = [(r, off, wid) for r, off, wid in days["2026-12-09"]
                          if wid not in ("inc-contacts-timeout", "inc-mcp-error")] + replaced
    picks = {"inc-contacts-timeout": [r for r, _o, w in replaced if w == "inc-contacts-timeout"],
             "inc-rate-limit": model_pick,
             "inc-mcp-error": [r for r, _o, w in replaced if w == "inc-mcp-error"]}
    return {"days": days, "windows": window_info, "picks": picks, "used": used, "user_draw_days": user_draw_days,
            "user_walk": user_walk}


def assign_users(days: dict, draw_days: dict, user_walk: list) -> dict:
    """One synthetic user per message. The seeded users stream walks the draw as it was (`draw_days`), so the other
    days keep their users; a message the replacement added takes its user from a stream of its own, walked in the
    replacement's order before the managing editor's fixed swap (`user_walk`), so the swap does not move any user."""
    ru = rng_for("users")
    user_of: dict[str, str] = {}
    for day in ["2026-12-07", "2026-12-08", "2026-12-09", "2026-12-10"]:
        for r, _off, *_rest in (draw_days.get(day, days[day])):
            if r["id"] not in user_of:
                user_of[r["id"]] = f"U{int(ru.integers(1, 201)):04d}"
    rw_users = rng_for("window_users")
    for day in ["2026-12-07", "2026-12-08", "2026-12-09", "2026-12-10"]:
        for r, _off, *_rest in (user_walk if day == "2026-12-09" else days[day]):
            if r["id"] not in user_of:
                user_of[r["id"]] = f"U{int(rw_users.integers(1, 201)):04d}"
    return user_of


# ----------------------------------------------------------------------------- writing

def write_jsonl(path: Path, rows: list[dict], ascii_only: bool = True) -> None:
    """One JSON object per line. Traffic and the en-US slice are ASCII; the locale files are UTF-8 (section 11)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=ascii_only, separators=(",", ":")) + "\n")


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii", newline="\n")


def scan_files(root: Path) -> dict:
    """Section 9 and the build's privacy scan, over every file under root (text only)."""
    email_rx = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    digits_rx = re.compile(r"\d{7,}")
    card_rx = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
    iban_rx = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
    url_rx = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|uk)\b")
    # The markers are joined in code so that this line does not match its own pattern.
    path_rx = re.compile("|".join(re.escape(m) for m in ("/" + "home/", "/" + "root/", "/" + "Users/")))
    found = Counter()
    files = [p for p in sorted(root.rglob("*"))
             if p.is_file() and p.name != "SHA256SUMS" and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in email_rx.finditer(text):
            if not m.group(1).endswith(".example"):
                found["email_off_example"] += 1
        found["digit_run_7plus"] += len(digits_rx.findall(text))
        for m in card_rx.finditer(text):
            digits = re.sub(r"[ -]", "", m.group(0))
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                found["luhn_card_like"] += 1
        for m in iban_rx.finditer(text):
            if iban_ok(m.group(0)):
                found["iban_like"] += 1
        found["url_like"] += len(url_rx.findall(text))
        found["home_or_root_path"] += len(path_rx.findall(text))
    found["files_scanned"] = len(files)
    return dict(found)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--massive-tar", required=True, type=Path)
    ap.add_argument("--massive-repo", required=True, type=Path)
    ap.add_argument("--slurp-license", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args(argv)
    out: Path = args.out.resolve()

    names, traffic, tools, prices = load_config()
    massive = read_massive(args.massive_tar)
    en = massive["en"]
    en_by_id = {r["id"]: r for r in en}
    if len(en) != 16521 or len(en_by_id) != 16521:
        raise SystemExit("en-US must hold 16,521 unique records")
    scen_seen = Counter(r["scenario"] for r in en)
    if len(scen_seen) != 18 or set(scen_seen) != set(SCENARIO_ROUTES):
        raise SystemExit("scenario table does not cover the 18 MASSIVE scenarios exactly")

    # 1. names rule (PL-30, section 2.2)
    removed, hits = names_rule(en, names)
    alone = Counter(c for cl in hits.values() for c in cl)
    first = Counter(removed.values())
    kept = [r for r in en if r["id"] not in removed]
    after_names = len(kept)

    # 2. duplicates, then partitions (section 2.2)
    uniq, dup_count = dedupe(kept)
    part = Counter(r["partition"] for r in uniq)
    train = sorted([r for r in uniq if r["partition"] == "train"], key=lambda r: int(r["id"]))
    test = sorted([r for r in uniq if r["partition"] == "test"], key=lambda r: int(r["id"]))
    train_keys = {" ".join(r["utt"].lower().split()) for r in train}
    cross = sum(1 for r in test if " ".join(r["utt"].lower().split()) in train_keys)
    if cross:
        raise SystemExit("an utterance is in both train and test after de-duplication")

    # 3. the schedules (sections 2.8, 2.9)
    sched = draw_schedules(test, train, en_by_id, traffic)
    user_of = assign_users(sched["days"], sched["user_draw_days"], sched["user_walk"])
    scheduled_ids = sorted({r["id"] for d in sched["days"].values() for r, *_ in d}, key=int)

    # 4. checks on the schedules
    base1_ids = {r["id"] for r, _ in sched["days"]["2026-12-08"]}
    paired_ids = {r["id"] for r, _ in sched["days"]["2026-12-08-paired"]}
    if not paired_ids <= base1_ids or len(paired_ids) != 60:
        raise SystemExit("the paired subset must be 60 messages of baseline day 1")
    for day, items in sched["days"].items():
        if day in ("2026-12-08-paired",):
            continue
        ids = [r["id"] for r, *_ in items]
        if len(set(ids)) != len(ids):
            raise SystemExit(f"duplicate message on {day}")
    per_day_sets = {d: {r["id"] for r, *_ in v} - (REPEATED_ON_INCIDENT if d == "2026-12-09" else set())
                    for d, v in sched["days"].items() if d != "2026-12-08-paired"}
    days_list = sorted(per_day_sets)
    for i, a in enumerate(days_list):
        for b in days_list[i + 1:]:
            if per_day_sets[a] & per_day_sets[b]:
                raise SystemExit(f"an utterance is on two days: {a} and {b}")
    dev_routes = {SCENARIO_ROUTES[r["scenario"]] for r, _ in sched["days"]["2026-12-07"]}
    if dev_routes != set(ROUTE_TOOLS) or len(sched["days"]["2026-12-07"]) != 20:
        raise SystemExit("the dev day needs 20 messages covering all seven routes")
    for w in WINDOWS:
        msgs = [(r, o) for r, o, wid in sched["days"]["2026-12-09"] if wid == w["id"]]
        if len(msgs) != w["n"]:
            raise SystemExit(f"window {w['id']} holds {len(msgs)} messages")
        for r, off in msgs:
            if not (w["start"] * 1_000_000 <= off < w["end"] * 1_000_000):
                raise SystemExit("window message outside its window")
            if w["pool"] == "email_contact" and SCENARIO_ROUTES[r["scenario"]] != "messages":
                raise SystemExit("window message does not reach find_contact")
            if w["pool"] == "model_route" and SCENARIO_ROUTES[r["scenario"]] == "fallback":
                raise SystemExit("window 2 message would not reach the model")

    # 5. the MASSIVE slices (the records the schedules use, and the locales restricted to the baseline day)
    slice_rows = [en_by_id[i] for i in scheduled_ids]
    slice_rows = [{k: r[k] for k in ("id", "partition", "scenario", "intent", "utt", "annot_utt")} for r in slice_rows]
    locale_rows = {}
    for loc in LOCALES:
        by_id = {r["id"]: r for r in massive["locales"][loc]}
        if len(by_id) != 16521 or [r["id"] for r in massive["locales"][loc]] != [r["id"] for r in en]:
            raise SystemExit(f"{loc}: ids do not match en-US in count or order (M-4)")
        for i in base1_ids:
            if by_id[i]["scenario"] != en_by_id[i]["scenario"] or by_id[i]["partition"] != en_by_id[i]["partition"]:
                raise SystemExit(f"{loc}: partition or scenario differs from en-US for id {i}")
        rows = []
        for r in sorted((en_by_id[i] for i in base1_ids), key=lambda x: int(x["id"])):
            loc_r = by_id.get(r["id"])
            if loc_r is None:
                raise SystemExit(f"{loc}: id {r['id']} missing")
            rows.append({"id": r["id"], "utt": loc_r["utt"]})
        locale_rows[loc] = rows

    # 6. the traffic files, ASCII (section 11)
    traffic_rows: dict[str, list[dict]] = {}
    for day, items in sched["days"].items():
        rows = []
        date = "2026-12-08" if day == "2026-12-08-paired" else day
        for r, off, *rest in sorted(items, key=lambda x: (x[1], int(x[0]["id"]))):
            if not r["utt"].isascii():
                raise SystemExit("non-ASCII utterance in the traffic")
            rows.append({"message_id": r["id"], "arrival": iso_at(date, off), "user_id": user_of[r["id"]],
                         "scenario": r["scenario"], "text": r["utt"]})
        traffic_rows[day] = rows

    # 7. tool records, users, contacts, routes
    users = build_users()
    contacts = build_contacts()
    records = build_tool_records(users)

    authored = check_authored(uniq, test, [r["text"] for rows in traffic_rows.values() for r in rows])
    if {r["id"] for r in slice_rows} != set(scheduled_ids):
        raise SystemExit("the en-US slice does not hold exactly the scheduled ids")

    # 8. the names rule must leave none of its names in what ships
    text_rx = term_regex(names["text_terms"] + names.get("scan_text_terms", []))
    brand_rx = term_regex(names["brand_slot_terms"])
    leaks = 0
    for r in slice_rows:
        leaks += bool(text_rx.search(norm(r["utt"])))
        leaks += any(t in names["name_slot_types"] for t, _ in SLOT_RE.findall(r["annot_utt"]))
        leaks += any(t in names["brand_slot_types"] and brand_rx.search(norm(v))
                     for t, v in SLOT_RE.findall(r["annot_utt"]))
    if leaks or any(i in removed for i in scheduled_ids):
        raise SystemExit("names rule leaks into the shipped data")

    # 9. write
    out.mkdir(parents=True, exist_ok=True)
    (out / "massive").mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "massive" / "en-US.jsonl", slice_rows)
    for loc in LOCALES:
        write_jsonl(out / "massive" / f"{loc}.jsonl", locale_rows[loc], ascii_only=False)
    (out / "massive" / "LICENSE").write_bytes(massive["LICENSE"])
    (out / "massive" / "NOTICE.md").write_bytes(massive["NOTICE"])
    slurp = args.slurp_license.read_bytes()
    if sha256_bytes(slurp) != SLURP_SHA256:
        raise SystemExit("SLURP licence text does not match its SHA-256")
    (out / "massive" / "SLURP-LICENSE.txt").write_bytes(slurp)
    code_lic = (args.massive_repo / "LICENSE.txt").read_bytes()
    (out / "massive" / "CODE-LICENSE.txt").write_bytes(code_lic)
    write_text(out / "massive" / "removed.tsv",
               "id\tfirst_clause\tall_clauses\n" + "".join(
                   f"{i}\t{removed[i]}\t{','.join(hits[i])}\n" for i in sorted(removed, key=int)))
    for day, rows in traffic_rows.items():
        write_jsonl(out / "traffic" / f"{day}.jsonl", rows)
    write_jsonl(out / "records" / "calendar.jsonl", records["calendar"])
    write_jsonl(out / "records" / "alarms.jsonl", records["alarms"])
    write_jsonl(out / "records" / "lists.jsonl", records["lists"])
    write_jsonl(out / "records" / "contacts.jsonl", contacts)
    write_text(out / "users.csv", "user_id,given_name,surname,email\n" + "".join(
        f"{u['user_id']},{u['given']},{u['surname']},{u['email']}\n" for u in users))
    write_text(out / "routes.toml", routes_toml())
    write_text(out / "incidents.toml", incidents_toml(sched))

    # 10. counts computed at build (PL-39, section 4.5)
    counts = build_counts(en, removed, hits, alone, first, after_names, dup_count, uniq, train, test, part,
                          cross, sched, traffic)
    counts["authored"] = authored
    write_text(out / "counts.toml", counts_toml(counts))
    write_text(out / "massive" / "SOURCE.md", source_md(names, counts, massive))

    # 11. the privacy scan, then the checksums of everything the build wrote
    scan = scan_files(out)
    sums = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            sums.append(f"{sha256_bytes(p.read_bytes())}  {p.relative_to(out).as_posix()}")
    write_text(out / "SHA256SUMS", "\n".join(sums) + "\n")

    summary(counts, scan, out)
    return 0 if scan.get("email_off_example", 0) == 0 and scan.get("luhn_card_like", 0) == 0 \
        and scan.get("iban_like", 0) == 0 and scan.get("home_or_root_path", 0) == 0 else 1


# ----------------------------------------------------------------------------- text outputs

def check_authored(pool: list[dict], test: list[dict], traffic_texts: list[str]) -> dict:
    """Plan section 4.7 and 2.7: the prompts' text is checked against the graded utterances and the traffic, and the
    knowledge base's count and length. Raises SystemExit on any failure."""
    ex = tomllib.loads((HERE / "routing_examples.toml").read_text(encoding="utf-8"))["example"]
    routes = sorted(e["route"] for e in ex)
    if routes != sorted(["calendar", "alarms", "lists", "messages", "devices", "media"]):
        raise SystemExit("routing_examples.toml must hold one example for each of the six routes except fallback")
    pol = parse_policies((HERE / "policies.md").read_text(encoding="utf-8"))
    if "Fallback reply" not in pol or "Answer rules" not in pol:
        raise SystemExit("policies.md needs the sections 'Fallback reply' and 'Answer rules'")
    prompt_texts = [e["text"] for e in ex] + list(pol.values())
    # "calendar" and "lists" are both a scenario label and a route name, and the route prompt lists route names.
    labels_rx = term_regex([s for s in SCENARIO_ROUTES if s not in ("calendar", "lists")])
    graded_norm = {norm(r["utt"]) for r in test}
    pool_norm = {norm(r["utt"]) for r in pool}
    for t in prompt_texts:
        if labels_rx.search(norm(t)):
            raise SystemExit(f"a prompt names a scenario label: {t[:60]!r}")
    for e in ex:
        if norm(e["text"]) in pool_norm:
            raise SystemExit("a routing example is an utterance in the pool")
    subs = 0
    for t in prompt_texts:
        nt = norm(t)
        for u in traffic_texts:
            if len(u) >= 20 and norm(u) in nt:
                subs += 1
    if subs:
        raise SystemExit("a prompt quotes a traffic utterance")
    articles = sorted((HERE / "articles").glob("help-*.md"))
    groups = Counter(p.stem.split("-")[1] for p in articles)
    if len(articles) != 30 or groups != Counter({"messages": 10, "devices": 10, "media": 10}):
        raise SystemExit("the knowledge base needs 30 articles, ten in each of messages, devices and media")
    words = {}
    for p in articles:
        n = len(p.read_text(encoding="utf-8").split())
        words[p.stem] = n
        if not 80 <= n <= 200:
            raise SystemExit(f"{p.name} has {n} words; 80 to 200 are required")
    return {"routing_examples": len(ex), "prompt_texts": len(prompt_texts), "articles": len(articles),
            "article_words_min": min(words.values()), "article_words_max": max(words.values()),
            "graded_norm": len(graded_norm), "pool_norm": len(pool_norm)}


def parse_policies(text: str) -> dict:
    sections, key = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            key = line[3:].strip()
            sections[key] = ""
        elif key is not None:
            sections[key] += line + "\n"
    return {k: v.strip() for k, v in sorted(sections.items())}


def routes_toml() -> str:
    lines = ["# Generated by build_case.py from SCENARIO_ROUTES (plan section 2.3). One row per MASSIVE scenario.", ""]
    for route in sorted(ROUTE_TOOLS):
        tool, retrieves = ROUTE_TOOLS[route]
        lines += ["[[route]]", f"name = {toml_str(route)}", f"tool = {toml_str(tool) if tool else chr(34)*2}",
                  f"retrieves = {'true' if retrieves else 'false'}", ""]
    for scen in sorted(SCENARIO_ROUTES):
        lines += ["[[scenario]]", f"name = {toml_str(scen)}", f"route = {toml_str(SCENARIO_ROUTES[scen])}", ""]
    return "\n".join(lines) + "\n"


def incidents_toml(sched: dict) -> str:
    lines = ["# Generated by build_case.py. Injected faults, labelled injected (section 2.9). Times are UTC on 2026-12-09.",
             ""]
    for w in sched["windows"]:
        ids = sorted((r["id"] for r, _o, wid in sched["days"]["2026-12-09"] if wid == w["id"]), key=int)
        start = iso_at("2026-12-09", w["start"] * 1_000_000)
        end = iso_at("2026-12-09", w["end"] * 1_000_000)
        lines += ["[[incident]]", f"id = {toml_str(w['id'])}", f"kind = {toml_str(w['kind'])}",
                  f"start = {toml_str(start)}", f"end = {toml_str(end)}", "injected = true",
                  f"fault = {toml_str(w['fault'])}", f"message_ids = [{', '.join(toml_str(i) for i in ids)}]", ""]
    return "\n".join(lines) + "\n"


def build_counts(en, removed, hits, alone, first, after_names, dup_count, uniq, train, test, part, cross, sched,
                 traffic) -> dict:
    test_route = Counter(SCENARIO_ROUTES[r["scenario"]] for r in test)
    test_scen = Counter(r["scenario"] for r in test)
    n_test = len(test)
    shares = {rt: test_route[rt] / n_test for rt in ROUTE_TOOLS}
    est = traffic["estimates"]
    per_msg = {
        "fallback": est["fallback_s"], "tool": est["tool_routes_s"], "retrieve": est["retrieving_routes_s"],
    }
    tool_routes = ["calendar", "alarms", "lists"]
    retr = ["messages", "devices", "media"]
    weights = {r: ("fallback" if r == "fallback" else "tool" if r in tool_routes else "retrieve") for r in ROUTE_TOOLS}
    mean_s = sum(shares[r] * per_msg[weights[r]] for r in ROUTE_TOOLS)
    base_route = Counter(SCENARIO_ROUTES[r["scenario"]] for r, _ in sched["days"]["2026-12-08"])
    base_mean = sum(base_route[r] * per_msg[weights[r]] for r in ROUTE_TOOLS) / 200
    recs_s = {
        "dev-day": 20 * mean_s, "http-replay": 5 * mean_s, "baseline-day-1": 200 * mean_s,
        "release-prompt-v2": 60 * mean_s + 60 * est["r1_extra_s"], "release-top-8": 60 * mean_s + 60 * est["r3_extra_s"],
        "release-cache-off": 60 * mean_s + 60 * est["r4_extra_s"], "incident-day": 60 * mean_s,
        "baseline-day-2": 80 * mean_s,
    }
    total_4b_s = sum(recs_s.values())
    retrieving_n = sum(test_route[r] for r in retr)
    counts = {
        "massive_records": len(en),
        "names_after_clauses": after_names,
        "names_clause_alone": dict(sorted(alone.items())),
        "names_clause_first": dict(sorted(first.items())),
        "names_removed_union": len(removed),
        "dedupe_copies": dup_count,
        "pool_unique": len(uniq),
        "pool_train": len(train),
        "pool_test": len(test),
        "pool_dev_set_aside": sum(1 for r in uniq if r["partition"] == "dev"),
        "cross_partition": cross,
        "test_by_route": {r: test_route[r] for r in sorted(ROUTE_TOOLS)},
        "test_by_scenario": {s: test_scen[s] for s in sorted(test_scen)},
        "datetime_in_test": test_scen["datetime"],
        "email_contact_in_test": sum(1 for r in test if r["scenario"] == "email" and r["intent"] in EMAIL_CONTACT_INTENTS),
        "retrieving_test": retrieving_n,
        "retrieving_share": retrieving_n / n_test,
        "fallback_share": test_route["fallback"] / n_test,
        "tool_route_share": sum(test_route[r] for r in tool_routes) / n_test,
        "mean_seconds_per_message_mix": mean_s,
        "baseline1_mean_seconds_per_message": base_mean,
        "baseline1_route": {r: base_route[r] for r in sorted(ROUTE_TOOLS)},
        "hours_baseline_day_1_mix": 200 * mean_s / 3600,
        "recording_hours_4b": {k: v / 3600 for k, v in recs_s.items()},
        "recording_hours_4b_total": total_4b_s / 3600,
        "recording_messages_4b": 20 + 5 + 200 + 60 + 60 + 60 + 60 + 80,
    }
    return counts


def counts_toml(c: dict) -> str:
    lines = ["# Generated by build_case.py. Counts the plan marks computed at build (PL-39, section 4.5).", ""]
    lines += [f"{k} = {fmt_val(v)}" for k, v in c.items() if not isinstance(v, dict)]
    lines.append("")
    for k, v in c.items():
        if isinstance(v, dict):  # tables come last, so no scalar falls under a table header
            lines.append(f"[{k}]")
            lines += [f"{json.dumps(kk)} = {fmt_val(vv)}" for kk, vv in v.items()]
            lines.append("")
    return "\n".join(lines) + "\n"


def fmt_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6f}"
    if isinstance(v, int):
        return str(v)
    return toml_str(str(v))


def source_md(names: dict, c: dict, massive: dict) -> str:
    rm = c["names_clause_first"]
    lines = [
        "# MASSIVE 1.1 slices for the Ambrewick case",
        "",
        "Generated by `examples/data/case/build_case.py`. The rows below are the records the book's schedules use, and the",
        "five locale files for the cost chapter's baseline day, restricted to the same ids.",
        "",
        "## Source",
        "",
        f"- Dataset: Amazon MASSIVE 1.1 (`amazon-massive-dataset-1.1.tar.gz`), `data/en-US.jsonl` and the five locale files.",
        f"- Tarball URL: {MASSIVE_URL}",
        f"- Tarball SHA-256: `{MASSIVE_SHA256}` ({MASSIVE_BYTES:,} bytes)",
        f"- HTTP `Last-Modified`: {MASSIVE_LAST_MODIFIED}",
        f"- Repository: {MASSIVE_REPO} at commit `{MASSIVE_COMMIT}`.",
        "- Records: 16,521 per locale, with the same ids in the same order in every locale.",
        "",
        "## Licences",
        "",
        "- Data: CC BY 4.0 (`LICENSE` and `NOTICE.md` here, copied from the tarball). Attribution is required, and so is a",
        "  note of changes: this slice keeps the en-US rows and the five locales for the same ids, and removes utterances",
        "  under the names rule below.",
        "- Code: Apache-2.0 (`CODE-LICENSE.txt`, copied from the repository, line 5: \"License for `massive` repo and code,",
        "  Apache 2.0\").",
        "- SLURP (the English seed of MASSIVE): `SLURP-LICENSE.txt`. Its textual part is CC BY 4.0.",
        "",
        "## What was removed, and why",
        "",
        "The names rule (plan section 2.2, PL-30) and the M-6 rule remove an utterance that names a real company, product,",
        "app, assistant or broadcaster, or that is a spelled-out web address. The lists are in `examples/data/case/names.toml`.",
        "The ids removed are in `removed.tsv` (id and clause only, never the text).",
        "",
        f"- Utterances in the dataset: {c['massive_records']:,}.",
        f"- Removed by clause, first match: a name slot {rm.get('a_name_slot', 0)}; the text list {rm.get('b_text', 0)};"
        f" a brand in a device, radio, podcast or game slot {rm.get('c_brand_slot', 0)}; the M-6 URL id"
        f" {rm.get('m6_url_id', 0)}; a spelled-out domain {rm.get('m6_url_phrase', 0)}.",
        f"- Removed in all: {c['names_removed_union']:,}, leaving {c['names_after_clauses']:,} before de-duplication.",
        f"- Exact duplicates after removal (case-folded, whitespace-trimmed; the first copy by id is kept): {c['dedupe_copies']}.",
        f"- Pool: {c['pool_unique']:,} unique utterances; train {c['pool_train']:,}, test {c['pool_test']:,}, and the dev"
        f" split is set aside.",
        "",
        "Persons, places, creative works and sports teams are kept. They are MASSIVE's own person and place values, labelled",
        "personal-looking in chapter 12 (PL-33).",
        "",
        "## Citations",
        "",
        "- FitzGerald, J., et al. (2022). MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset with 51"
        " Typologically-Diverse Languages. arXiv:2204.08582.",
        "- Bastianelli, E., Vanzo, A., Swietojanski, P., and Rieser, V. (2020). SLURP: A Spoken Language Understanding Resource"
        " Package. EMNLP 2020. aclanthology.org/2020.emnlp-main.588.",
        "",
    ]
    return "\n".join(lines) + "\n"


def summary(c: dict, scan: dict, out: Path) -> None:
    print(f"output folder: {out.name}")
    print(f"names removed (union): {c['names_removed_union']}; first clause: {c['names_clause_first']}")
    print(f"names removed (alone): {c['names_clause_alone']}")
    print(f"dedupe copies: {c['dedupe_copies']}; pool unique {c['pool_unique']}; train {c['pool_train']}; "
          f"test {c['pool_test']}; cross-partition {c['cross_partition']}")
    print(f"test by route: {c['test_by_route']}")
    print(f"datetime in test: {c['datetime_in_test']}; email contact in test: {c['email_contact_in_test']}")
    print(f"retrieving share {c['retrieving_share']:.4f}; fallback share {c['fallback_share']:.4f}; "
          f"tool share {c['tool_route_share']:.4f}")
    print(f"mean seconds per message (mix) {c['mean_seconds_per_message_mix']:.3f}; baseline-1 mix "
          f"{c['baseline1_mean_seconds_per_message']:.3f}")
    print(f"baseline day 1 hours (mix) {c['hours_baseline_day_1_mix']:.3f}")
    print(f"4B recording hours total {c['recording_hours_4b_total']:.3f} over {c['recording_messages_4b']} messages")
    print(f"scan: {scan}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
