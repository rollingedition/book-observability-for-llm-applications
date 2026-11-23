"""The book's own OTLP/HTTP receiver, on loopback (plan section 6, chapter 5).

It takes the two encodings this book sends: protobuf, which the Python exporter posts, and the JSON that
`export.to_otlp_json` writes. Each request's spans become rows in `export.spans_to_rows`' shape, so the rows it stores
can be compared with the exporter's own rows (plan section 5, chapter 5). It binds port 0 on 127.0.0.1. The port is
never printed; the examples read it from `endpoint` and use it only to send.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.trace import SpanKind

from lib.assistant import AGENT, RELEASES
from lib.tracing import span

_KIND_NAME = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS_NAME = {0: "UNSET", 1: "OK", 2: "ERROR"}
_PATH = "/v1/traces"
RELEASE = RELEASES["r0"]  # the release the dev-day recording was made with


def loopback_only():
    """Make sure requests to 127.0.0.1 never go through a proxy the environment names.

    Why: a sandbox's HTTP proxy would take the request to the local server and refuse it. Adding loopback to NO_PROXY
    changes nothing else, and it applies to every client in this process, the exporter and the MLflow client included.
    """
    names = [part for part in os.environ.get("NO_PROXY", "").split(",") if part]
    for host in ("127.0.0.1", "localhost"):
        if host not in names:
            names.append(host)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(names)


def _plain_value(value):
    """Return one OTLP AnyValue (protobuf message or JSON wrapper) as the Python value the exporter started from."""
    if isinstance(value, dict):  # JSON: {"stringValue": ...}, {"intValue": "5"}, ...
        if "arrayValue" in value:
            return [_plain_value(item) for item in value["arrayValue"]["values"]]
        if "intValue" in value:
            return int(value["intValue"])
        for key in ("stringValue", "boolValue", "doubleValue"):
            if key in value:
                return value[key]
        raise ValueError("an attribute value this receiver does not read")
    kind = value.WhichOneof("value")  # protobuf
    if kind == "array_value":
        return [_plain_value(item) for item in value.array_value.values]
    if kind in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(value, kind)
    raise ValueError("an attribute value this receiver does not read")


def _attributes(pairs):
    """Turn OTLP key-value pairs (protobuf or JSON) into a dict sorted by key, as export.py's rows keep them."""
    result = {}
    for pair in pairs:
        if isinstance(pair, dict):
            result[pair["key"]] = _plain_value(pair["value"])
        else:
            result[pair.key] = _plain_value(pair.value)
    return dict(sorted(result.items()))


def _row(*, trace, span, parent, name, kind, start, end, status, attributes, events, resource, scope):
    """One stored row, with the same keys and sort key as export.spans_to_rows.

    The trace and span IDs are hex in OTLP. The sequence numbers are their integer values, which is the sequence
    number the sequential generator gave them. The telemetry.sdk.* resource values are dropped, as the exporter's rows
    drop them (plan rule 6).
    """
    return {
        "trace_seq": int(trace, 16), "span_seq": int(span, 16),
        "trace_id": trace, "span_id": span, "parent_span_id": parent or None,
        "name": name, "kind": _KIND_NAME[kind], "start_ns": start, "end_ns": end,
        "status": _STATUS_NAME[status], "attributes": attributes, "events": events,
        "resource": {key: value for key, value in sorted(resource.items()) if not key.startswith("telemetry.sdk.")},
        "scope": scope,
    }


def _from_protobuf(body):
    """Rows from one ExportTraceServiceRequest. Raises ValueError when the bytes are not that message."""
    request = ExportTraceServiceRequest.FromString(body)
    rows = []
    for resource_spans in request.resource_spans:
        resource = _attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                rows.append(_row(
                    trace=span.trace_id.hex(), span=span.span_id.hex(), parent=span.parent_span_id.hex(),
                    name=span.name, kind=span.kind, start=span.start_time_unix_nano, end=span.end_time_unix_nano,
                    status=span.status.code, attributes=_attributes(span.attributes),
                    events=[{"name": event.name, "time_ns": event.time_unix_nano,
                             "attributes": _attributes(event.attributes)} for event in span.events],
                    resource=resource, scope=scope_spans.scope.name))
    return rows


def _from_json(body):
    """Rows from one OTLP/JSON request, as export.to_otlp_json writes it: hex IDs, nanoseconds as strings."""
    request = json.loads(body)
    rows = []
    for resource_spans in request["resourceSpans"]:
        resource = _attributes(resource_spans["resource"]["attributes"])
        for scope_spans in resource_spans["scopeSpans"]:
            for span in scope_spans["spans"]:
                rows.append(_row(
                    trace=span["traceId"], span=span["spanId"], parent=span.get("parentSpanId"),
                    name=span["name"], kind=span["kind"], start=int(span["startTimeUnixNano"]),
                    end=int(span["endTimeUnixNano"]), status=span["status"]["code"],
                    attributes=_attributes(span["attributes"]),
                    events=[{"name": event["name"], "time_ns": int(event["timeUnixNano"]),
                             "attributes": _attributes(event["attributes"])} for event in span["events"]],
                    resource=resource, scope=scope_spans["scope"]["name"]))
    return rows


def decode(body, content_type):
    """Return the stored rows of one request body. A content type the book does not send raises ValueError."""
    if content_type.startswith("application/x-protobuf"):
        return _from_protobuf(body)
    if content_type.startswith("application/json"):
        return _from_json(body)
    raise ValueError(f"content type {content_type} is not one this receiver reads")


class _Handler(BaseHTTPRequestHandler):
    """Accepts POST /v1/traces and answers 200 with the request's own content type, as OTLP/HTTP asks."""

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path != _PATH:
            self.send_error(404)
            return
        try:
            rows = decode(body, content_type)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_error(400, str(error))
            return
        self.server.receiver._store(content_type, rows)
        reply = b"{}" if content_type.startswith("application/json") else b""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, format, *args):  # the examples print nothing from the server
        return


class Receiver:
    """A receiver on 127.0.0.1, port 0, serving in a daemon thread. Use `stop()` when the example is done.

    Each request is stored when it arrives, before the reply is sent, so an exporter that waits for its reply (the
    synchronous processor does) can rely on the spans being stored by the time its `span.end()` returns.
    """

    def __init__(self):
        loopback_only()
        self._lock = threading.Lock()
        self._requests = []  # (content type, rows) in arrival order
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.receiver = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def endpoint(self):
        """The URL the exporter posts to, with the path /v1/traces. It holds the port, so do not print it."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{_PATH}"

    @property
    def address(self):
        """The host the receiver is bound to, for the loopback check."""
        return self._server.server_address[0]

    def _store(self, content_type, rows):
        with self._lock:
            self._requests.append((content_type, rows))

    def requests(self):
        """The (content type, span count) of each request, in arrival order."""
        with self._lock:
            return [(content_type, len(rows)) for content_type, rows in self._requests]

    def rows(self):
        """Every stored row, in the stable order of export.spans_to_rows (rule 4)."""
        with self._lock:
            stored = [row for _, rows in self._requests for row in rows]
        return sorted(stored, key=lambda row: (row["trace_seq"], row["start_ns"], row["span_seq"]))

    def stop(self):
        """Stop serving and join the thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def start():
    """Start a receiver on loopback and return it."""
    return Receiver()


DEV_DAY_RECORDING = Path(__file__).resolve().parents[1] / "data" / "recorded" / "dev-day" / "traces.jsonl"
SAMPLE_MESSAGE = "809"  # a dev-day message whose route and answer calls the recording holds


def _recorded_answer(message_id):
    """The answer call's recorded counts for one dev-day message: prompt, completion and cached tokens."""
    for line in DEV_DAY_RECORDING.read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        if call["message_id"] == message_id and call["step"] == "answer":
            return call
    raise KeyError(f"no recorded answer call for message {message_id}")


def message_spans(tracer, clock):
    """Record one dev-day message's three spans, with the virtual clock's times: the agent's root, its chat call and a
    tool call. The chat call's counts are the recording's, so the same numbers reach every backend this chapter sends
    to. Returns (root, chat, tool), all ended."""
    call = _recorded_answer(SAMPLE_MESSAGE)
    root = span(tracer, f"invoke_agent {AGENT}", clock.at(36000), clock.at("36000.4"), kind=SpanKind.INTERNAL,
                attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT})
    chat = span(tracer, f"chat {RELEASE.model}", clock.at(36000), clock.at("36000.3"), parent=root,
                kind=SpanKind.CLIENT, attributes={
                    "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
                    "gen_ai.request.model": RELEASE.model,
                    "gen_ai.usage.input_tokens": call["prompt_tokens"],
                    "gen_ai.usage.output_tokens": call["completion_tokens"],
                    "gen_ai.usage.cache_read.input_tokens": call["cached_tokens_derived"]})
    tool = span(tracer, "execute_tool find_contact", clock.at("36000.3"), clock.at("36000.4"), parent=root,
                kind=SpanKind.INTERNAL, attributes={"gen_ai.operation.name": "execute_tool",
                                                     "gen_ai.tool.name": "find_contact"})
    return root, chat, tool
