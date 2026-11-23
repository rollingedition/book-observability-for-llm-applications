"""What the scanner counts in telemetry (chapter 12), and the replay of a day's model calls through the official
package, so a span can hold captured content.

Three groups of kinds, kept apart in every table and the figure:
- the synthetic users' items, which redaction must remove: `user_id`, `user_name` and `email`. The scanner looks for the
  case data's own values as exact strings (the IDs, names and addresses in `data/case/users.csv`), not for the patterns
  `redact.py` uses, so a zero after redaction is evidence. The example asserts that none is left after redaction;
- `stacktrace`: an `exception.stacktrace` event. A stack trace names no user, but its paths can name the account that
  ran the process, which is personal data, and they reveal the machine. It is counted apart from the personal items;
- `slot:*`: MASSIVE's personal-looking values (person, place, relation, email address and personal information) found
  in captured content. They are the dataset's own request text, so the scanner measures them and never asserts zero.

`scan_rows` counts matches (items); `spans_holding` counts the rows that hold at least one. Both take rows in
lib/export.py's shape.
"""
import json
import logging
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import httpx
import openai
import opentelemetry.sdk.trace as sdk_trace
from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.genai._upload import upload_completion_hook  # the entry point the hook variable names

from lib import otlp
from lib.assistant import RELEASES
from lib.case import CASE_DIR, load_users
from lib.clock import VirtualClock
from lib.dayrun import RECORDINGS, run_day_traced
from lib.export import spans_to_rows
from lib.ids import SequentialIds
from lib.report import fmt_int
from lib.redact import STACKTRACE, RedactingProcessor, has_absolute_path
from lib.tracing import make_provider

SYNTHETIC = ("user_id", "user_name", "email")
SLOT_LABELS = ("person", "place_name", "relation", "email_address", "personal_info")
KINDS = SYNTHETIC + ("stacktrace",) + tuple(f"slot:{label}" for label in SLOT_LABELS)
MASSIVE = CASE_DIR / "massive" / "en-US.jsonl"
RECORDED = Path(__file__).resolve().parents[1] / "data" / "recorded"
ANNOTATION = re.compile(r"\[(\w+) : ([^\]]+)\]")  # MASSIVE's slot labels in an annotated utterance
CONTENT_KEYS = ("gen_ai.input.messages", "gen_ai.output.messages")  # the attributes a captured message goes in
CAPTURE = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
HOOK, UPLOAD = "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH"


@lru_cache(maxsize=1)
def slot_patterns() -> dict[str, re.Pattern]:
    """One pattern per slot label: whole words that the English annotations give as that label's values.

    Why values, not positions: the captured text has no slot marks, so a value found in a message's text is counted as
    that label. A word that is a value of one label and a word of another text can be counted too; the residue is
    therefore printed as a measure, not as proof of a person's data (PL-34).
    """
    values: dict[str, set[str]] = {label: set() for label in SLOT_LABELS}
    for line in MASSIVE.read_text(encoding="utf-8").splitlines():
        for label, value in ANNOTATION.findall(json.loads(line)["annot_utt"]):
            if label in values:
                values[label].add(value.strip())
    # A label with no value in the English annotations (email_address) gets a pattern that matches nothing: an empty
    # alternation would match at every word boundary.
    return {label: re.compile(r"\b(?:" + "|".join(re.escape(v) for v in sorted(words, key=len, reverse=True)) + r")\b",
                              re.IGNORECASE) if words else re.compile(r"(?!)")
            for label, words in values.items()}


def _strings(row: dict):
    """Each string a row holds: its attribute values and its events' attribute values."""
    for value in row["attributes"].values():
        if isinstance(value, str):
            yield value
    for event in row["events"]:
        for value in event["attributes"].values():
            if isinstance(value, str):
                yield value


def _captured_text(row: dict):
    """The text of a row's captured messages, without the system text: the app's own prompt names no person, and it
    repeats words such as "assistant" that are also slot values. What remains is the user's text and the replies."""
    for key in CONTENT_KEYS:
        for message in json.loads(row["attributes"].get(key, "[]")):
            if message["role"] != "system":
                for part in message["parts"]:
                    yield part.get("content", "")


def _alternation(values) -> str:
    """The values as one regex alternation, longest first, each escaped."""
    return "|".join(re.escape(value) for value in sorted(values, key=len, reverse=True))


@lru_cache(maxsize=1)
def case_patterns() -> dict[str, re.Pattern]:
    """One pattern per synthetic kind: the case's own values, as exact strings from data/case/users.csv.

    Why exact strings: the redactor's patterns could miss a value they were not written for, and a scan that used the
    same patterns would then count the same miss as a zero. Whole tokens for IDs and names, so "U0001" is not found
    inside a longer token; an address must match as written, and names are case-sensitive, as the lists write them.
    """
    users = load_users()
    names = {word for user in users for word in (user.given_name, user.surname)}
    return {
        "user_id": re.compile(rf"(?<!\w)(?:{_alternation(u.user_id for u in users)})(?!\w)"),
        "user_name": re.compile(rf"(?<!\w)(?:{_alternation(names)})(?!\w)"),
        "email": re.compile(rf"(?<![\w.+-])(?:{_alternation(u.email for u in users)})(?![\w-])"),
    }


def _count(row: dict) -> Counter:
    """The matches a single row holds, by kind."""
    counts = Counter()
    for event in row["events"]:
        if STACKTRACE in event["attributes"]:
            counts["stacktrace"] += 1
    patterns = case_patterns()
    for text in _strings(row):
        for kind in SYNTHETIC:
            counts[kind] += len(patterns[kind].findall(text))
    for text in _captured_text(row):
        for label, pattern in slot_patterns().items():
            counts[f"slot:{label}"] += len(pattern.findall(text))
    return counts


def scan_rows(rows) -> Counter:
    """The matches in all rows, by kind: every kind of KINDS is present, zero when a row holds none of it."""
    total = Counter({kind: 0 for kind in KINDS})
    for row in rows:
        total.update(_count(row))
    return total


def spans_holding(rows) -> Counter:
    """The number of rows that hold at least one match of each kind, by kind (every kind of KINDS is present)."""
    holding = Counter({kind: 0 for kind in KINDS})
    for row in rows:
        holding.update(kind for kind, count in _count(row).items() if count)
    return holding


def is_json(text: str) -> bool:
    """Whether a captured value still parses as JSON, which the messages' attributes are when whole."""
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def print_hook_run(run: str, spans, uploads: dict) -> None:
    """Print what one hooked run left, each label naming its run: spans, references, inline content and uploads."""
    refs = sum(any(k.endswith("_ref") for k in s.attributes) for s in spans)
    inline = sum(any(k in CONTENT_KEYS for k in s.attributes) for s in spans)
    print(f"spans exported, {run}:", fmt_int(len(spans)))
    print(f"spans with a content reference, {run}:", fmt_int(refs))
    print(f"spans with inline content, {run}:", fmt_int(inline))
    print(f"uploaded inputs, {run}:", fmt_int(len(uploads["inputs"])))
    print(f"uploaded outputs, {run}:", fmt_int(len(uploads["outputs"])))


def label(kind: str) -> str:
    """A kind's words for a printed line. The label has no colon of its own, since `re-check` reads a line's label as
    the text before its first colon, so a bound value needs a label such as "person slot", not "slot:person"."""
    return kind.replace("slot:", "") + (" slot" if kind.startswith("slot:") else "")


def of_service(rows, service: str) -> list[dict]:
    """The rows one service wrote, by its service name."""
    return [row for row in rows if row["resource"]["service.name"] == service]


def stack_traces(rows) -> list[str]:
    """The text of each stack trace on a set of rows' events."""
    return [event["attributes"][STACKTRACE] for row in rows for event in row["events"] if STACKTRACE in event["attributes"]]


def absolute_paths(rows) -> int:
    """The strings in all rows that still name a folder of the machine that wrote them (redact.has_absolute_path)."""
    return sum(has_absolute_path(text) for row in rows for text in _strings(row))


def assistant_rows(day: str, redact: bool = False) -> list[dict]:
    """The assistant's own spans for one traffic day, as rows: dayrun's replay, which keeps no content. With `redact`,
    each span passes a RedactingProcessor as it ends, the processor the chapter describes.

    The unredacted spans still reach dayrun's own exporter; the redacted copies reach this function's exporter, and
    only those are returned.
    """
    if not redact:
        return run_day_traced(day, live=False).spans
    exporter = InMemorySpanExporter()
    run_day_traced(day, live=False, processors=[RedactingProcessor(exporter)])
    return spans_to_rows(exporter)


def model_calls(day: str) -> list[dict]:
    """The recorded model calls of one traffic day (the route and answer calls, not the tool calls), in the order they
    were made. `day` is a recording's folder name, such as "baseline-day-1"."""
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((RECORDED / day / "calls").glob("*.json"))]
    return sorted((row for row in rows if row["kind"] == "model"), key=lambda row: row["seq"])


def captured_call(row: dict) -> None:
    """Send one recorded call through the openai client, which the caller has instrumented, and let it answer with the
    recorded text. The request is the recorded one; the model is the release's, as the recording was made with it.

    Why one reply per client: the client answers the call it was made for, so no file lookup can give another call's
    reply. The httpx logger is set to WARNING, as lib/http_replay.py sets it, so the request line is not printed.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    body = {key: value for key, value in row["request"].items() if key != "kind"}
    reply = {"id": f"call-{row['seq']}", "object": "chat.completion", "created": 0, "model": RELEASES["r0"].model,
             "choices": [{"index": 0, "finish_reason": row["finish_reason"],
                          "message": {"role": "assistant", "content": row["text"]}}],
             "usage": {"prompt_tokens": row["prompt_tokens"], "completion_tokens": row["completion_tokens"],
                       "total_tokens": row["prompt_tokens"] + row["completion_tokens"]}}
    client = openai.OpenAI(base_url="http://localhost:8000/v1", api_key="local", max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(
                               lambda request: httpx.Response(200, json=reply))))
    client.chat.completions.create(**dict(body, model=RELEASES["r0"].model))


def captured_day(day: str, mode: str = "SPAN_ONLY", redact: bool = False) -> list[dict]:
    """The model calls of one traffic day (`day` is a key of lib/dayrun.RECORDINGS), each made through the official
    package with content capture set to `mode`, as rows in lib/export.py's shape. The assistant's own spans are a
    separate set (dayrun's); this is the package's. With `redact`, the spans pass a RedactingProcessor before export.

    Why the clock is patched and then restored: the package's spans take their times from the SDK, so virtual times make
    the rows the same on every run (the rule chapter 3 uses); the patch is removed afterwards for the other examples.
    """
    clock = VirtualClock(day)
    real_time_ns = sdk_trace.time_ns
    sdk_trace.time_ns = lambda: clock.advance(1_000_000)
    os.environ[CAPTURE] = mode  # the package reads the mode when it is instrumented
    exporter = InMemorySpanExporter()
    if redact:  # a provider with no exporter of its own: the processor is the only path to the exporter
        provider = TracerProvider(id_generator=SequentialIds(),
                                  resource=Resource({"service.name": "ambrewick-assistant"}))
        provider.add_span_processor(RedactingProcessor(exporter))
    else:
        provider = make_provider("ambrewick-assistant", RELEASES["r0"].service_version, "captured", clock,
                                 SequentialIds(), exporter, "https://opentelemetry.io/schemas/1.45.0")
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        for call in model_calls(RECORDINGS[day]):
            captured_call(call)
    finally:
        instrumentor.uninstrument()
        del os.environ[CAPTURE]
        sdk_trace.time_ns = real_time_ns
    return spans_to_rows(exporter)


def live_rows(day: str) -> list[dict]:
    """The rows the book's receiver stores for one live day: the assistant's spans and the contacts server's spans, both
    exported over OTLP to the receiver (chapter 5). Content capture is whatever the environment says; this helper sets
    none, so the rows hold no content. One live run per process: dayrun sets the process's tracer provider."""
    receiver = otlp.start()
    try:
        run_day_traced(day, live=True, otlp_endpoint=receiver.endpoint)
        return receiver.rows()
    finally:
        receiver.stop()


def hooked_calls(calls: list[dict], sampler, mode, folder: Path) -> tuple[list, dict]:
    """Send recorded calls through the package with the upload hook the environment names, and wait for its uploads.
    Returns the spans the exporter holds, and the texts of the uploaded input and output files, by kind. `folder` is the
    store; it is made here, and the hook writes into it.

    Why this entry point: the environment names the hook, and `upload_completion_hook` returns that hook itself, so the
    caller can shut it down and wait, since the hook uploads on threads of its own. `mode` None leaves capture unset.
    """
    folder.mkdir(parents=True)  # the hook checks that it can write here when it is made
    os.environ[HOOK] = "upload"
    os.environ[UPLOAD] = folder.as_uri()
    if mode:
        os.environ[CAPTURE] = mode
    hook = upload_completion_hook()
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler, id_generator=SequentialIds(),
                              resource=Resource({"service.name": "ambrewick-assistant"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, completion_hook=hook)
    try:
        for call in calls:
            captured_call(call)
    finally:
        instrumentor.uninstrument()
        hook.shutdown(timeout_sec=30)
        for key in (HOOK, UPLOAD, CAPTURE):
            os.environ.pop(key, None)
    # The package writes a call's input and output messages as two files; the system text is inside the inputs.
    files = sorted(path for path in folder.rglob("*") if path.is_file())   # sorted, so the order never depends on the file system
    texts = {kind: [p.read_text(encoding="utf-8") for p in files if p.name.endswith(f"_{kind}.json")]
             for kind in ("inputs", "outputs")}
    return exporter.get_finished_spans(), texts
