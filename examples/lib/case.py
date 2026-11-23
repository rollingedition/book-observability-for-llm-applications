"""Loaders for the Ambrewick case's data, read from examples/data/case/ (plan section 6, case.py).

Every function here reads files the book ships and returns what it reads, in a stable order, so that output built from
it does not depend on the order of a dict or a set. Nothing here reads a clock or prints (plan section 6's rules).
"""
from lib import SEED  # noqa: F401  (importing lib fixes threads and seeds before anything else in this module loads)

import csv
import datetime as dt
import functools
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parents[1] / "data" / "case"
_UTC = dt.timezone.utc
_ARRIVAL_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class Message:
    """One message of a traffic day. The arrival is an exact count of nanoseconds from midnight UTC of its day."""

    message_id: str
    arrival_ns: int
    user_id: str
    scenario: str
    text: str


@dataclass(frozen=True)
class User:
    """A synthetic user: names from the fixed lists, an address on the reserved .example domain."""

    user_id: str
    given_name: str
    surname: str
    email: str


@dataclass(frozen=True)
class Article:
    """One knowledge base article. The title is its first line, without the markdown heading mark."""

    article_id: str
    group: str
    title: str
    text: str


@dataclass(frozen=True)
class Route:
    """One of the seven routes: its tool (None when it has none) and whether it retrieves from the knowledge base."""

    name: str
    tool: str | None
    retrieves: bool


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _arrival_ns(day: str, iso: str) -> int:
    """Convert an ISO 8601 UTC arrival to nanoseconds from midnight of its day. Microseconds are exact, so the
    conversion is an integer multiplication and loses nothing."""
    moment = dt.datetime.strptime(iso, _ARRIVAL_FORMAT).replace(tzinfo=_UTC)
    midnight = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_UTC)
    return (moment - midnight) // dt.timedelta(microseconds=1) * 1000


def load_messages(day: str) -> list[Message]:
    """The messages of one traffic day, sorted by arrival and then by message ID.

    `day` is a traffic file's name: "2026-12-07" (dev day), "2026-12-08" (baseline day 1), "2026-12-08-paired" (the
    60-message paired subset of baseline day 1, with the same arrivals), "2026-12-09" (incident day) or "2026-12-10"
    (baseline day 2). The sort is the stable key the book's tables assume (plan section 1.4, rule 4)."""
    date = day[:10]
    rows = _read_jsonl(CASE_DIR / "traffic" / f"{day}.jsonl")
    msgs = [Message(r["message_id"], _arrival_ns(date, r["arrival"]), r["user_id"], r["scenario"], r["text"])
            for r in rows]
    return sorted(msgs, key=lambda m: (m.arrival_ns, int(m.message_id)))


def load_users() -> list[User]:
    """The 200 synthetic users, sorted by user ID. Each address is on users.example, so none reaches a mailbox."""
    with (CASE_DIR / "users.csv").open(encoding="utf-8", newline="") as f:
        rows = [User(r["user_id"], r["given_name"], r["surname"], r["email"]) for r in csv.DictReader(f)]
    return sorted(rows, key=lambda u: u.user_id)


def load_articles() -> list[Article]:
    """The knowledge base's articles, sorted by ID (help-{group}-{nn}). The book's retriever sorts its own ties by ID
    too, so the same article order is used everywhere it is printed."""
    out = []
    for path in sorted((CASE_DIR / "articles").glob("help-*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        title = lines[0].lstrip("# ").strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        group = path.stem.split("-")[1]
        out.append(Article(path.stem, group, title, body))
    return sorted(out, key=lambda a: a.article_id)


@functools.lru_cache(maxsize=1)
def _routes_table() -> dict:
    return tomllib.loads((CASE_DIR / "routes.toml").read_text(encoding="utf-8"))


def load_routes() -> list[Route]:
    """The seven routes of section 2.3, sorted by name. A route with no tool has tool None."""
    rows = [Route(r["name"], r["tool"] or None, bool(r["retrieves"])) for r in _routes_table()["route"]]
    return sorted(rows, key=lambda r: r.name)


def scenario_to_route(scenario: str) -> str:
    """The route a MASSIVE scenario goes to (section 2.3). A scenario with no row, or with two, raises: the table is
    the one source for the route check, and a scenario must go to exactly one route."""
    hits = [row["route"] for row in _routes_table()["scenario"] if row["name"] == scenario]
    if len(hits) != 1:
        raise KeyError(f"scenario {scenario!r} maps to {len(hits)} routes; it must map to exactly one")
    return hits[0]


def load_locale(locale: str, ids) -> list[str]:
    """The utterance text of each id in one locale file, in ascending numeric ID order (chapter 6 joins locales by id,
    M-4). `ids` may be any iterable of ids; duplicates are not allowed, and an id the file lacks raises."""
    given = [str(i) for i in ids]
    if len(set(given)) != len(given):
        raise ValueError("ids must be unique")
    wanted = sorted(given, key=int)
    by_id = {r["id"]: r["utt"] for r in _read_jsonl(CASE_DIR / "massive" / f"{locale}.jsonl")}
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise KeyError(f"{locale}: {len(missing)} ids are not in the shipped slice")
    return [by_id[i] for i in wanted]


def load_policies() -> dict[str, str]:
    """The sections of policies.md, keyed by heading and sorted by key: the fallback reply and the answer rules, the
    texts the prompt and the fallback route share (plan section 4.7)."""
    sections: dict[str, str] = {}
    key = None
    for line in (CASE_DIR / "policies.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            key = line[3:].strip()
            sections[key] = ""
        elif key is not None:
            sections[key] += line + "\n"
    return {k: v.strip() for k, v in sorted(sections.items())}


_RECORD_FILES = {"calendar": "calendar.jsonl", "alarms": "alarms.jsonl", "lists": "lists.jsonl",
                 "contacts": "contacts.jsonl"}
_RECORD_KEYS = {
    "calendar": lambda r: (r["user_id"], r["date"], r["time"], r["title"]),
    "alarms": lambda r: (r["user_id"], r["time"], r["label"]),
    "lists": lambda r: (r["user_id"], r["list"]),
    "contacts": lambda r: (r["user_id"], r["name"], r["email"]),
}


def load_records(kind: str) -> list[dict]:
    """The in-process tools' fixed records: kind is "calendar", "alarms", "lists" or "contacts". Sorted by user and
    then by the record's own fields, so a tool's answer never depends on file order (the contacts server reads the
    same file)."""
    if kind not in _RECORD_FILES:
        raise KeyError(f"unknown record kind {kind!r}")
    rows = _read_jsonl(CASE_DIR / "records" / _RECORD_FILES[kind])
    return sorted(rows, key=_RECORD_KEYS[kind])


def load_tool_durations() -> dict[str, int]:
    """Modelled durations of the three in-process tools, in nanoseconds, sorted by tool name (plan section 2.6). The
    book labels these as modelled, never measured."""
    table = tomllib.loads((CASE_DIR / "tool_durations.toml").read_text(encoding="utf-8"))
    return {name: int(table[name]["duration_ns"]) for name in sorted(table)}
