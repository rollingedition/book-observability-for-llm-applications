"""The injected incidents of the incident day (plan sections 2.9 and 6).

The faults run in the book's own code: the contacts server (`contacts_server.py`) and the tool wrapper in `tools.py`.
The schedule is the case's `incidents.toml`, written by build_case.py. Every span a fault touches carries
`ambrewick.incident.injected = true`, so a reader never takes a fault for a real failure.

A window is active from its start, inclusive, to its end, exclusive.
"""

from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass

from lib.case import CASE_DIR

VIRTUAL_TIME_META = "ambrewick/virtual_time_ns"
"""The `_meta` key under which the client sends a request's virtual time, in nanoseconds, to the contacts server."""

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_SCHEDULE = CASE_DIR / "incidents.toml"


@dataclass(frozen=True)
class Incident:
    """One injected window: its id, its virtual start and end in nanoseconds, and its kind."""

    incident_id: str
    start_ns: int
    end_ns: int
    kind: str


def _ns(stamp: str) -> int:
    """An ISO 8601 UTC time as integer nanoseconds since the epoch. Integer arithmetic only, so no rounding."""
    when = dt.datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        raise ValueError(f"{stamp} has no time zone")
    delta = when - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def load_incidents() -> list[Incident]:
    """The schedule, sorted by start time then id. Every row must be labelled injected."""
    table = tomllib.loads(_SCHEDULE.read_text(encoding="utf-8"))
    incidents = []
    for row in table["incident"]:
        if row["injected"] is not True:
            raise ValueError(f"{row['id']} is not labelled as injected")
        incidents.append(Incident(row["id"], _ns(row["start"]), _ns(row["end"]), row["kind"]))
    return sorted(incidents, key=lambda i: (i.start_ns, i.incident_id))


def active(now_ns: int) -> Incident | None:
    """The injected window open at `now_ns` (virtual time), or None. Windows do not overlap in the schedule."""
    for incident in load_incidents():
        if incident.start_ns <= now_ns < incident.end_ns:
            return incident
    return None


class RateLimitError(Exception):
    """The model endpoint's injected 429 (inc-rate-limit). Its class name is the attempt span's `error.type`."""

    def __init__(self, incident: Incident):
        super().__init__("429 Too Many Requests (injected)")
        self.incident = incident


def rate_limited(now_ns: int) -> Incident | None:
    """The rate-limit window open at `now_ns`, or None: the model endpoint answers 429 to every request inside it."""
    incident = active(now_ns)
    return incident if incident is not None and incident.kind == "rate-limit" else None


CONTACT_FAULTS = ("contacts-timeout", "mcp-error")
"""The window kinds the contacts server's faults run in. A model's rate limit does not touch a contacts call."""


def contact_fault(now_ns: int) -> Incident | None:
    """The contacts fault open at `now_ns`, or None: the window that makes a `find_contact` call fail or time out. Every
    span such a call touches is labelled with this incident (the attempt, the MCP client span, the server's spans)."""
    incident = active(now_ns)
    return incident if incident is not None and incident.kind in CONTACT_FAULTS else None
