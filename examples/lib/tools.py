"""The tools the assistant can call (plan section 2.6).

Eleven tools in four routes. The calendar, alarm and list tools read and change an in-memory copy of the fixed records
in lib/case, made fresh for each message, and run in the assistant's process. `send_message` records a message in that
copy; nothing is sent. `find_contact` calls the contacts server through a contacts object (the recording's live client,
or the replay of its recorded answers). Argument schemas are pydantic models, so a malformed call fails the same way on
every run, and comes back as a structured reply.

Durations of the in-process tools are modelled (tool_durations.toml) and labelled as modelled in the text.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lib import inject
from lib.case import load_records
from lib.clock import VirtualClock
from lib.context import carry
from lib.contacts_server import SPANS_ENV, TRACES_ENDPOINT_ENV
from lib.inject import VIRTUAL_TIME_META

INCIDENT_KEY = otel_context.create_key("ambrewick.incident")  # the injected incident a call's spans are labelled with

_EXAMPLES_DIR = Path(__file__).resolve().parents[1]  # the folder that holds the `lib` package
REQUEST_TIMEOUT_NS = 2_000_000_000  # the client's read timeout for find_contact
ATTEMPTS = 2  # a tool call's attempts: the first, then one retry (the assistant's tool step makes them)
RETRY_DELAY_NS = 500_000_000  # the tool step waits this long before the retry (virtual time, not slept)
TIMEOUT_CODE = -32001  # the SDK's code for a read timeout (B-52); the MCP spec assigns no code to a local timeout
MCP_TOOLS = frozenset({"find_contact"})  # the tools the contacts server serves; their errors are MCP errors


class AttemptFailed(Exception):
    """One attempt at a tool call that ended in an exception the client raised, not in a reply. `error` is that
    exception (for find_contact, the MCPError the SDK raised); `duration_ns` is the attempt's virtual length, which for a
    read timeout is REQUEST_TIMEOUT_NS. The assistant records the attempt and decides whether to retry."""

    def __init__(self, error: Exception, duration_ns: int):
        super().__init__(str(error))
        self.error = error
        self.duration_ns = duration_ns

    @property
    def error_type(self) -> str:
        """The exception's class name, the value the attempt's `error.type` carries."""
        return type(self.error).__name__

    @property
    def code(self) -> int | None:
        """The JSON-RPC error code of an MCPError, or None for any other exception."""
        return self.error.error.code if isinstance(self.error, MCPError) else None

    @property
    def timed_out(self) -> bool:
        return self.code == TIMEOUT_CODE


@dataclass(frozen=True)
class ContactReply:
    """What the contacts server returned for one `find_contact` call."""

    output: Any
    is_error: bool
    duration_ns: int


class Contacts(Protocol):
    """Anything that answers `find_contact`: the recording's live MCP client, or a replay. One call is one attempt; a
    call that the client raises on comes back as AttemptFailed, and the caller decides about a retry."""

    def find(self, user_id: str, name: str) -> ContactReply: ...


class IncidentMarker(SpanProcessor):
    """Labels a span with its incident when the context it starts in names one (INCIDENT_KEY). `LiveContacts.find` sets the
    key around a call an injected fault touches, so the call's MCP client span carries the same marker as its attempt."""

    def on_start(self, span: Span, parent_context=None) -> None:
        incident = otel_context.get_value(INCIDENT_KEY, parent_context)
        if incident is not None:
            span.set_attribute("ambrewick.incident.id", incident.incident_id)
            span.set_attribute("ambrewick.incident.injected", True)

    def on_end(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class LiveContacts:
    """`find_contact` through the contacts server over stdio, with one server process and one MCP session for the whole
    run (plan section 2.6). The client's event loop runs in a thread, so the synchronous `Tools` can call it. Use
    `close()` (or `with`) to end the session and the server.

    The server writes its spans to `spans_path`, or, with `otlp_endpoint`, exports them to that OTLP endpoint (chapter
    5's receiver). The session's thread starts in a copy of the caller's context, so its discovery call joins the span
    that is current when this object is made. `at(clock)` makes the calls inside it use another clock, so one session
    can serve several messages, each with its own clock (`run_day_traced`).

    `find` is one attempt: it sends the clock's virtual time under `VIRTUAL_TIME_META`, which the server's fault hook
    reads, and raises `AttemptFailed` when the client raises (a read timeout is MCPError -32001). The retry is the
    assistant's tool step, so each attempt has its own span and the clock moves between them. `timer_ns` measures a
    call that answers, where it runs (the recorder passes `time.perf_counter_ns`); with none, that duration is 0.
    """

    def __init__(self, clock: VirtualClock, spans_path: str | Path | None = None, timer_ns=None,
                 otlp_endpoint: str | None = None):
        self._clock = clock
        self._timer_ns = timer_ns
        env = {}
        if spans_path is not None:
            env[SPANS_ENV] = str(spans_path)
        if otlp_endpoint is not None:
            env[TRACES_ENDPOINT_ENV] = otlp_endpoint
        if not env:
            raise ValueError("give a spans path, an OTLP endpoint, or both")
        self._params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lib.contacts_server"],
            cwd=str(_EXAMPLES_DIR),
            env=env,
        )
        self._ready = threading.Event()
        self._closing: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Client | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=carry(lambda: asyncio.run(self._serve())), daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    async def _serve(self) -> None:
        """Hold the client open on this loop until `close()` asks it to stop."""
        self._loop = asyncio.get_running_loop()
        self._closing = asyncio.Event()
        try:
            async with Client(self._params) as client:
                self._client = client
                self._ready.set()
                await self._closing.wait()
        except Exception as exc:
            self._error = exc
            self._ready.set()

    @contextmanager
    def at(self, clock: VirtualClock) -> Iterator[LiveContacts]:
        """Make the calls in the block use `clock`'s virtual time, then go back to the session's own clock."""
        own, self._clock = self._clock, clock
        try:
            yield self
        finally:
            self._clock = own

    def __enter__(self) -> LiveContacts:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Stop the session and the server process, then join the thread."""
        if self._loop is not None and self._closing is not None:
            self._loop.call_soon_threadsafe(self._closing.set)
        self._thread.join()

    def _elapsed(self, started: int) -> int:
        return self._timer_ns() - started if self._timer_ns is not None else 0

    async def _call(self, user_id: str, name: str, now_ns: int, incident) -> Any:
        """The call, run on the session's loop. An incident in force is set in this task's context while the call runs,
        so the IncidentMarker labels the MCP client span the call starts (the attempt's own span is labelled by the
        assistant)."""
        token = otel_context.attach(otel_context.set_value(INCIDENT_KEY, incident))
        try:
            return await self._client.call_tool(
                "find_contact",
                {"user_id": user_id, "name": name},
                read_timeout_seconds=REQUEST_TIMEOUT_NS / 1_000_000_000,
                meta={VIRTUAL_TIME_META: now_ns},
            )
        finally:
            otel_context.detach(token)

    def find(self, user_id: str, name: str) -> ContactReply:
        started = self._timer_ns() if self._timer_ns is not None else 0
        now = self._clock.now_ns
        future = asyncio.run_coroutine_threadsafe(
            self._call(user_id, name, now, inject.contact_fault(now)), self._loop)
        try:
            result = future.result()
        except MCPError as err:
            if err.error.code == TIMEOUT_CODE:
                raise AttemptFailed(err, REQUEST_TIMEOUT_NS) from err
            raise AttemptFailed(err, self._elapsed(started)) from err
        measured = self._elapsed(started)
        text = result.content[0].text if result.content else ""
        output = text if result.is_error else json.loads(text)
        return ContactReply(output=output, is_error=result.is_error, duration_ns=measured)


@dataclass(frozen=True)
class ToolResult:
    """One tool call's outcome. `malformed` is set when the arguments fail the schema (plan section 2.6), and then
    `error_type` is the exception's class name (`ValidationError`). `is_error` is set for a malformed call and for a call
    the records cannot act on. `not_found` marks that second case: the tool looked and found nothing (an ID that is not
    there), a normal answer rather than a failure (PL-68). Either way the model sees `output`, a structured reply, and the
    message goes on."""

    output: Any
    is_error: bool
    malformed: bool
    duration_ns: int
    modelled: bool
    not_found: bool = False
    error_type: str | None = None


def _check_day(value: str) -> str:
    dt.date.fromisoformat(value)  # raises ValueError for anything but a real calendar day
    return value


def _check_time(value: str) -> str:
    if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError("the time must be HH:MM, on a 24-hour clock")
    return value


def _clean(value: str, limit: int) -> str:
    """The text with its spaces collapsed; empty text and text over `limit` characters are refused."""
    text = " ".join(value.split())
    if not text:
        raise ValueError("must not be empty")
    if len(text) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return text


class ListEventsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    date: str = Field(description="The day in UTC, as YYYY-MM-DD.")

    @field_validator("date")
    @classmethod
    def _iso_day(cls, value: str) -> str:
        return _check_day(value)


class AddEventArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    date: str = Field(description="The day in UTC, as YYYY-MM-DD.")
    time: str = Field(description="The start time, HH:MM on a 24-hour clock.")
    title: str = Field(description="What the event is called, up to 60 characters.")

    @field_validator("date")
    @classmethod
    def _iso_day(cls, value: str) -> str:
        return _check_day(value)

    @field_validator("time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        return _check_time(value)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _clean(value, 60)


class RemoveEventArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    event_id: str = Field(min_length=1, max_length=20, description="The event_id that list_events gave.")


class ListAlarmsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str


class SetAlarmArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    time: str = Field(description="The alarm time, HH:MM on a 24-hour clock.")
    label: str = Field(description="The alarm's label, up to 40 characters.")

    @field_validator("time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        return _check_time(value)

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _clean(value, 40)


class RemoveAlarmArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    alarm_id: str = Field(min_length=1, max_length=20, description="The alarm_id that list_alarms gave.")


class ListItemsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    user_id: str
    list_name: Literal["shopping", "packing", "to do"] = Field(alias="list", description="The list to read.")


class ChangeItemArgs(BaseModel):
    """The arguments of add_item and remove_item: a list and one item's text."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    user_id: str
    list_name: Literal["shopping", "packing", "to do"] = Field(alias="list", description="The list to change.")
    item: str = Field(description="The item's text, up to 40 characters.")

    @field_validator("item")
    @classmethod
    def _item(cls, value: str) -> str:
        return _clean(value, 40)


class FindContactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    name: str = Field(description="The contact's name, as the user says it.")


class SendMessageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    contact_id: str = Field(min_length=1, max_length=100,
                            description="The contact's address, as find_contact gave it.")
    text: str = Field(description="The message, up to 500 characters.")

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        return _clean(value, 500)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    in_process: bool


TOOL_SPECS = (
    Tool("list_events", "List the user's events on one day (UTC). Up to five, by time.", ListEventsArgs, True),
    Tool("add_event", "Add an event to the user's calendar on one day (UTC).", AddEventArgs, True),
    Tool("remove_event", "Remove one of the user's events, by its event_id.", RemoveEventArgs, True),
    Tool("list_alarms", "List the user's alarms. Up to five, by time.", ListAlarmsArgs, True),
    Tool("set_alarm", "Set a new alarm for the user.", SetAlarmArgs, True),
    Tool("remove_alarm", "Remove one of the user's alarms, by its alarm_id.", RemoveAlarmArgs, True),
    Tool("list_items", "List one of the user's lists: shopping, packing or to do. Up to ten items.", ListItemsArgs,
         True),
    Tool("add_item", "Add an item to one of the user's lists: shopping, packing or to do.", ChangeItemArgs, True),
    Tool("remove_item", "Remove an item from one of the user's lists: shopping, packing or to do.", ChangeItemArgs,
         True),
    Tool("find_contact", "Find a contact by name. Up to three contacts, with their names and addresses.",
         FindContactArgs, False),
    Tool("send_message", "Record a message to one of the user's contacts, named by the address find_contact gave. "
                         "The message is recorded, not sent.", SendMessageArgs, True),
)
TOOL_NAMES = tuple(sorted(t.name for t in TOOL_SPECS))
TOOLS = {t.name: t for t in TOOL_SPECS}  # the frozen interface's name: name -> spec (schema and kind)

# The tools each route offers (plan section 2.6). The first one is the route's read tool in routes.toml. A route not
# listed here offers no tool.
ROUTE_TOOLS = {
    "alarms": ("list_alarms", "set_alarm", "remove_alarm"),
    "calendar": ("list_events", "add_event", "remove_event"),
    "lists": ("list_items", "add_item", "remove_item"),
    "messages": ("find_contact", "send_message"),
}


def route_tools(route: str) -> tuple[str, ...]:
    """The tools a message's route offers, in the order the answer prompt lists them."""
    return ROUTE_TOOLS.get(route, ())


def _problems(err: ValidationError) -> list[dict]:
    """The field and the problem for each failed check. The value the model sent is not echoed back."""
    return [{"field": ".".join(str(part) for part in e["loc"]), "problem": e["msg"]} for e in err.errors()]


def _group_by_user(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["user_id"], []).append(row)
    return grouped


class Tools:
    """The tools, bound to the fixed records, the modelled durations and a contacts object.

    The records the tools act on are an in-memory copy of the seeded records, made fresh by `begin_message()` for each
    message. A change made by one message is never seen by another, so a replay answers the same way every time.
    """

    def __init__(self, durations: dict[str, int], contacts: Contacts | None):
        self._spec = {t.name: t for t in TOOL_SPECS}
        self._durations = dict(durations)
        self._contacts = contacts
        self._seed = {
            "calendar": _group_by_user(load_records("calendar")),
            "alarms": _group_by_user(load_records("alarms")),
            "lists": _group_by_user(load_records("lists")),
            "contacts": _group_by_user(load_records("contacts")),
        }
        self.begin_message()

    def begin_message(self) -> None:
        """Start a message from the seeded records: a fresh copy, with no messages sent yet."""
        self._records: dict[str, dict[str, list[dict]]] = copy.deepcopy(self._seed)
        self._sent: list[dict] = []
        self._count = 0

    @property
    def sent(self) -> list[dict]:
        """The messages this message has recorded, in order (copies)."""
        return [dict(m) for m in self._sent]

    def _new_id(self) -> str:
        self._count += 1
        return f"new-{self._count}"

    def description(self, name: str) -> str:
        return self._spec[name].description

    def args_schema(self, name: str) -> dict:
        """The JSON schema of a tool's arguments as the model sees them: user_id is left out, since the assistant
        supplies the message's own user (a model never names the user)."""
        schema = self._spec[name].args_model.model_json_schema(by_alias=True)
        schema["properties"].pop("user_id", None)
        schema["required"] = [r for r in schema.get("required", []) if r != "user_id"]
        return schema

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run a tool. Unknown names raise KeyError. Arguments that fail the schema come back malformed, with a
        structured reply the model can read; nothing here raises for bad arguments."""
        spec = self._spec[name]
        try:
            args = spec.args_model.model_validate(arguments)
        except ValidationError as err:
            return ToolResult(output={"error": "invalid_arguments", "fields": _problems(err)}, is_error=True,
                              malformed=True, duration_ns=0, modelled=spec.in_process,
                              error_type=type(err).__name__)
        if name == "find_contact":
            if self._contacts is None:
                raise RuntimeError("find_contact needs a contacts object")
            reply = self._contacts.find(args.user_id, args.name)
            return ToolResult(output=reply.output, is_error=reply.is_error, malformed=False,
                              duration_ns=reply.duration_ns, modelled=False)
        output, is_error = getattr(self, f"_{name}")(args)
        not_found = is_error and isinstance(output, dict) and output.get("error") == "not_found"
        return ToolResult(output=output, is_error=is_error, malformed=False,
                          duration_ns=self._durations[name], modelled=True, not_found=not_found)

    # --- calendar -----------------------------------------------------------------------------------------

    def _list_events(self, args: ListEventsArgs) -> tuple[Any, bool]:
        rows = [r for r in self._records["calendar"].get(args.user_id, []) if r["date"] == args.date]
        rows.sort(key=lambda r: (r["time"], r["title"], r["event_id"]))
        return [{"event_id": r["event_id"], "time": r["time"], "title": r["title"]} for r in rows[:5]], False

    def _add_event(self, args: AddEventArgs) -> tuple[Any, bool]:
        row = {"user_id": args.user_id, "date": args.date, "time": args.time, "title": args.title,
               "event_id": self._new_id()}
        self._records["calendar"].setdefault(args.user_id, []).append(row)
        view = {k: row[k] for k in ("event_id", "date", "time", "title")}
        return {"status": "added", "event": view}, False

    def _remove_event(self, args: RemoveEventArgs) -> tuple[Any, bool]:
        rows = self._records["calendar"].get(args.user_id, [])
        for index, row in enumerate(rows):
            if row["event_id"] == args.event_id:
                del rows[index]
                return {"status": "removed", "event_id": args.event_id}, False
        return {"error": "not_found", "what": "event"}, True

    # --- alarms -------------------------------------------------------------------------------------------

    def _list_alarms(self, args: ListAlarmsArgs) -> tuple[Any, bool]:
        rows = sorted(self._records["alarms"].get(args.user_id, []),
                      key=lambda r: (r["time"], r["label"], r["alarm_id"]))
        return [{"alarm_id": r["alarm_id"], "time": r["time"], "label": r["label"], "enabled": r["enabled"]}
                for r in rows[:5]], False

    def _set_alarm(self, args: SetAlarmArgs) -> tuple[Any, bool]:
        row = {"user_id": args.user_id, "time": args.time, "label": args.label, "enabled": True,
               "alarm_id": self._new_id()}
        self._records["alarms"].setdefault(args.user_id, []).append(row)
        view = {k: row[k] for k in ("alarm_id", "time", "label", "enabled")}
        return {"status": "set", "alarm": view}, False

    def _remove_alarm(self, args: RemoveAlarmArgs) -> tuple[Any, bool]:
        rows = self._records["alarms"].get(args.user_id, [])
        for index, row in enumerate(rows):
            if row["alarm_id"] == args.alarm_id:
                del rows[index]
                return {"status": "removed", "alarm_id": args.alarm_id}, False
        return {"error": "not_found", "what": "alarm"}, True

    # --- lists --------------------------------------------------------------------------------------------

    def _list_row(self, user_id: str, list_name: str) -> dict:
        """The user's row for one list. The generator gives every user all three lists, so the row is found."""
        rows = self._records["lists"].setdefault(user_id, [])
        for row in rows:
            if row["list"] == list_name:
                return row
        row = {"user_id": user_id, "list": list_name, "items": []}
        rows.append(row)
        return row

    def _list_items(self, args: ListItemsArgs) -> tuple[Any, bool]:
        row = self._list_row(args.user_id, args.list_name)
        return {"list": args.list_name, "items": sorted(row["items"])[:10]}, False

    def _add_item(self, args: ChangeItemArgs) -> tuple[Any, bool]:
        row = self._list_row(args.user_id, args.list_name)
        if any(item.casefold() == args.item.casefold() for item in row["items"]):
            return {"status": "already_listed", "list": args.list_name, "item": args.item}, False
        row["items"] = sorted(row["items"] + [args.item])
        return {"status": "added", "list": args.list_name, "item": args.item}, False

    def _remove_item(self, args: ChangeItemArgs) -> tuple[Any, bool]:
        row = self._list_row(args.user_id, args.list_name)
        for index, item in enumerate(row["items"]):
            if item.casefold() == args.item.casefold():
                del row["items"][index]
                return {"status": "removed", "list": args.list_name, "item": item}, False
        return {"error": "not_found", "what": "item"}, True

    # --- messages -----------------------------------------------------------------------------------------

    def _send_message(self, args: SendMessageArgs) -> tuple[Any, bool]:
        contacts = self._records["contacts"].get(args.user_id, [])
        match = next((c for c in contacts if c["email"].casefold() == args.contact_id.casefold()), None)
        if match is None:
            return {"error": "not_found", "what": "contact"}, True
        message = {"message_id": self._new_id(), "contact_id": match["email"], "name": match["name"],
                   "text": args.text}
        self._sent.append(message)
        return {"status": "recorded", "message_id": message["message_id"], "name": match["name"]}, False
