"""The dev day's messages, replayed through the assistant: the setup chapter 1's examples share.

`run_day(day, message_ids)` answers every model call from that day's recording, gives each message its own virtual clock
starting at its arrival, and returns what each message did. `run_dev_day()` is `run_day` for the dev day, so chapter 1's
examples are unchanged. `run_day_traced` is the same replay with its spans kept, and `live=True` (the default) sends
`find_contact` to one contacts server over stdio for the whole run, started once inside a "contacts startup" span.
Each message's `tools/call` joins that message's trace. With `otlp_endpoint`, both processes export to that endpoint. `recorded_gap_ns()`
reads the recorder's own offsets, which the replay does not keep. The examples in chapter 1 depend on the signatures of
`run_dev_day`, `Run` and `recorded_gap_ns`.
"""
import json
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lib.assistant import RELEASES, Assistant, Outcome
from lib.bm25 import Retriever
from lib.case import Message, load_articles, load_messages, load_tool_durations
from lib.clock import VirtualClock
from lib.export import spans_to_rows
from lib.ids import SequentialIds
from lib.replay import Replay, ReplayContacts
from lib.tools import IncidentMarker, LiveContacts, Tools
from lib.tracing import make_provider

DAY, RELEASE = "2026-12-07", RELEASES["r0"]     # the dev day; the release the recording was made with
DATA = Path(__file__).resolve().parents[1] / "data" / "recorded"
RECORDING = DATA / "dev-day"
# The traffic days the recordings cover, each with its recording folder. Every one was made with release r0.
RECORDINGS = {
    "2026-12-07": "dev-day",
    "2026-12-08": "baseline-day-1",
    "2026-12-09": "incident-day",
    "2026-12-10": "baseline-day-2",
}


@dataclass(frozen=True)
class Run:
    """One message as the replay handled it. `clock` ends at the reply, so the message's time is its end minus arrival."""

    message: Message
    clock: VirtualClock
    arrived_ns: int
    outcome: Outcome

    @property
    def replied_ns(self) -> int:
        return self.clock.now_ns


def run_dev_day(message_ids=None) -> list[Run]:
    """Replay the dev day's messages, in arrival order, and return one Run for each (`run_day` for the dev day)."""
    return run_day(DAY, message_ids)


def run_day(day: str, message_ids=None) -> list[Run]:
    """Replay one traffic day's messages, in arrival order, and return one Run for each.

    `day` is a key of RECORDINGS. `message_ids` names the messages to replay; None means all of the day's messages.
    Each message gets a fresh VirtualClock at its arrival, so the answer prompt's date is the day and no message starts
    where another ended. Every model call is answered from the day's recording, and a call the recording lacks raises
    NotRecorded. A day with no recording raises KeyError.
    """
    return run_day_traced(day, message_ids, live=False).runs


@dataclass(frozen=True)
class Traced:
    """A replay with its spans. `runs` is what `run_day` gives. `spans` are the rows (lib.export.spans_to_rows) of the
    assistant's spans and the SDK's client spans; `server` are the contacts server's rows, in the same shape, read after
    its process exited. `server` is empty when the replay was not live, or when the server exported over OTLP."""

    runs: list[Run]
    spans: list[dict]
    server: list[dict]


def run_day_traced(day: str, message_ids=None, *, backend=None, live: bool = True,
                   otlp_endpoint: str | None = None, meter_provider=None, after_message=None,
                   processors=()) -> Traced:
    """Replay one traffic day's messages, as `run_day` does, and keep the spans on both sides.

    `backend` is the model: the day's recording by default. With `live`, each message's `find_contact` calls go to one
    contacts server over stdio, started once for the run, inside a "contacts startup" span. Each message calls it on
    its own clock, so the server's fault windows apply. Without `live`, the recording answers them. With
    `otlp_endpoint` (a live run only), the assistant also exports its spans there, and the server is given the endpoint
    and exports its own, so both processes' spans reach one receiver. A live run sets the process's global tracer
    provider, so the SDK's client spans reach `spans`; OpenTelemetry takes one such provider per process, so a second
    live run raises. Span IDs are sequential, and no ID or time from the server's clock is printed by the examples.

    `meter_provider` gives the assistant its live meter (its token metrics and durations, recorded as each operation
    ends). `after_message` is called with each message's `Run` once it is handled, so an example can read the meter at
    that point. Neither changes the spans. `processors` are added to the assistant's provider after its own exporter,
    so each sees every span as it ends (chapter 12's redacting processor).
    """
    if otlp_endpoint is not None and not live:
        raise ValueError("otlp_endpoint needs a live run: it is the second process's export")
    replay = Replay(DATA / RECORDINGS[day])
    backend = replay if backend is None else backend
    exporter = InMemorySpanExporter()
    provider = make_provider("ambrewick-assistant", RELEASE.service_version, f"{day}-traced",
                             VirtualClock(day), SequentialIds(), exporter)
    if otlp_endpoint is not None:
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    for processor in processors:
        provider.add_span_processor(processor)
    if live:
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            raise RuntimeError("a live run sets the process's tracer provider, which is set once per process")
        provider.add_span_processor(IncidentMarker())  # labels the MCP client span of a call an incident touches
        trace.set_tracer_provider(provider)  # the SDK makes its client span with the global provider
    tracer = provider.get_tracer("ambrewick.assistant")
    retriever = Retriever(load_articles(), RELEASE.top_k)
    runs, server = [], []
    with tempfile.TemporaryDirectory() as folder:
        spans_file = Path(folder) / "contacts-spans.jsonl"  # the server writes here; read once it has exited
        shared = _start_contacts(tracer, day, spans_file, otlp_endpoint) if live else None
        try:
            for message in load_messages(day):
                if message_ids is not None and message.message_id not in message_ids:
                    continue
                clock = VirtualClock(day, start_ns=message.arrival_ns)
                arrived = clock.now_ns
                with (shared.at(clock) if live else nullcontext(ReplayContacts(replay))) as answers:
                    tools = Tools(load_tool_durations(), answers)
                    assistant = Assistant(backend, retriever, tools, clock, tracer, RELEASE, meter_provider)
                    outcome = assistant.handle(message)
                runs.append(Run(message, clock, arrived, outcome))
                if after_message is not None:
                    after_message(runs[-1])
        finally:
            if shared is not None:
                shared.close()  # ends the session and the server, so the server's file is complete
        if live and spans_file.exists():
            server = [json.loads(line) for line in spans_file.read_text(encoding="utf-8").splitlines()]
    return Traced(runs, spans_to_rows(exporter), server)


def _start_contacts(tracer, day: str, spans_file: Path, otlp_endpoint: str | None) -> LiveContacts:
    """Start the run's one contacts server. Its discovery call joins this span's trace, so the run's startup is one
    trace of its own, labelled as the startup, and no message's trace holds a discovery call."""
    with tracer.start_as_current_span("contacts startup", attributes={"ambrewick.phase": "startup"}):
        return LiveContacts(VirtualClock(day), spans_file, otlp_endpoint=otlp_endpoint)


def recorded_gap_ns(message_id: str, first: str, second: str) -> int:
    """The time the recorder saw between the end of one model call and the start of the next, for one message.

    The replay gives the steps no gap, so the gap comes from the recording's own offsets: the second call's offset minus
    the first call's offset plus its duration. Both offsets count from the same origin, so the origin cancels out.
    """
    rows = [json.loads(line) for line in (RECORDING / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = {row["step"]: row for row in rows if row["kind"] == "model" and row["message_id"] == message_id}
    return calls[second]["offset_ns"] - (calls[first]["offset_ns"] + calls[first]["total_ns"])
