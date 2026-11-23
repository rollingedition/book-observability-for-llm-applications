"""The contacts team's MCP server (plan sections 2.6 and 2.9), run as `python -m lib.contacts_server` over stdio.

It has one tool, `find_contact`, which reads the synthetic contacts through `lib.case`. Stdout carries the protocol,
so the server prints nothing. Its spans go one of two ways, chosen by its environment:
- with `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`, it exports them over OTLP/HTTP to that
  endpoint (the book's receiver, in chapter 5);
- otherwise it writes them to a JSON-lines file named by `AMBREWICK_CONTACTS_SPANS`, which the assistant reads after the
  process exits (chapter 4).
Either way the processor is synchronous, so every span is sent or written before the process exits.

Fault hook: the client sends the request's virtual time in `_meta` under `inject.VIRTUAL_TIME_META`. If an injected
window is open at that time, the tool fails as the schedule says:
- `contacts-timeout`: the tool sleeps past the client's read timeout, so the client raises `MCPError` -32001;
- `mcp-error`: the tool raises, so the client gets an error result (`isError`).
Both are labelled `ambrewick.incident.injected = true` on the server's spans.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from collections.abc import Sequence

from mcp.server.mcpserver import Context, MCPServer
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import IdGenerator, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from lib.case import load_records
from lib.inject import VIRTUAL_TIME_META, Incident, contact_fault

SPANS_ENV = "AMBREWICK_CONTACTS_SPANS"
TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
SERVICE_NAME = "contacts-server"
SERVICE_INSTANCE_ID = "contacts-server-1"
FAULT_SLEEP_S = 3.0  # past the client's 2.0 s read timeout (the client gives up first)
MAX_CONTACTS = 3

mcp = MCPServer("contacts")
TOOL_LOGGER = "mcp.server.mcpserver.server"  # the logger the MCP server writes a crashed tool's traceback to


class InjectedFault(RuntimeError):
    """The contacts server's injected error (`mcp-error`). The MCP server reports it to the client as a crash, with the
    same reply the recording holds (`Error executing tool find_contact`), and logs its traceback. `QuietInjected` drops
    that traceback, and only that one: a crash of any other cause still writes its traceback to stderr (PL-74)."""


class QuietInjected(logging.Filter):
    """Drops a log record whose exception is, or was caused by, an InjectedFault."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        while exc is not None:
            if isinstance(exc, InjectedFault):
                return False
            exc = exc.__cause__ or exc.__context__
        return True


class JsonLinesExporter(SpanExporter):
    """Appends each finished span as one JSON object per line. The file is read after the process exits."""

    def __init__(self, path: str):
        self._path = path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with open(self._path, "a", encoding="utf-8") as fh:
            for span in spans:
                fh.write(json.dumps(_row(span), sort_keys=True) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _row(span: ReadableSpan) -> dict:
    """One span as a plain row: names, IDs, status, times and attributes."""
    parent = span.parent
    return {
        "name": span.name,
        "kind": span.kind.name,
        "trace_id": format(span.context.trace_id, "032x"),
        "span_id": format(span.context.span_id, "016x"),
        "parent_span_id": None if parent is None else format(parent.span_id, "016x"),
        "status": span.status.status_code.name,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "attributes": dict(sorted((span.attributes or {}).items())),
    }


class _SecureIds(IdGenerator):
    """Random IDs from `secrets`. `lib/__init__` seeds `random`, and the SDK's default generator draws from it, so two
    processes that import `lib` would issue the same span IDs and the client's and server's spans would collide."""

    def generate_trace_id(self) -> int:
        return secrets.randbits(128) or 1

    def generate_span_id(self) -> int:
        return secrets.randbits(64) or 1


def otlp_endpoint() -> str | None:
    """The OTLP endpoint this process was given, or None. The traces endpoint wins over the general one."""
    return os.environ.get(TRACES_ENDPOINT_ENV) or os.environ.get(ENDPOINT_ENV) or None


def _provider(exporter: SpanExporter) -> None:
    """Make this process's tracer provider, with one synchronous processor, so each span is exported when it ends."""
    resource = Resource.create({"service.name": SERVICE_NAME, "service.instance.id": SERVICE_INSTANCE_ID})
    provider = TracerProvider(resource=resource, id_generator=_SecureIds())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def install_tracing(spans_path: str) -> None:
    """Write each span to the JSON-lines file as it ends (no endpoint in the environment)."""
    _provider(JsonLinesExporter(spans_path))


def install_otlp() -> None:
    """Send each span to the endpoint in the environment, as it ends. OTLPSpanExporter() reads the endpoint itself,
    with its /v1/traces path added when the general variable is the one set."""
    _provider(OTLPSpanExporter())


def _virtual_time(ctx: Context) -> int | None:
    """The request's virtual time from its `_meta`, or None when the client sent none."""
    meta = ctx.request_context.meta or {}
    value = meta.get(VIRTUAL_TIME_META)
    return None if value is None else int(value)


def _label(span: trace.Span, incident: Incident) -> None:
    span.set_attribute("ambrewick.incident.id", incident.incident_id)
    span.set_attribute("ambrewick.incident.injected", True)


@mcp.tool()
def find_contact(user_id: str, name: str, ctx: Context) -> dict:
    """Find the user's contacts whose name contains the given name. Up to three, sorted by name."""
    now_ns = _virtual_time(ctx)
    incident = None if now_ns is None else contact_fault(now_ns)
    if incident is not None:
        _label(trace.get_current_span(), incident)  # the SDK's SERVER span for this request
    with trace.get_tracer(__name__).start_as_current_span("contacts.lookup") as span:
        span.set_attribute("contacts.query_length", len(name))
        if incident is not None:
            _label(span, incident)
            if incident.kind == "contacts-timeout":
                time.sleep(FAULT_SLEEP_S)
            elif incident.kind == "mcp-error":
                raise InjectedFault("contact store unavailable (injected)")
        needle = name.strip().casefold()
        found = [r for r in load_records("contacts") if r["user_id"] == user_id and needle in r["name"].casefold()]
        return {"contacts": [{"name": r["name"], "email": r["email"]} for r in found[:MAX_CONTACTS]]}


def main() -> None:
    logging.getLogger(TOOL_LOGGER).addFilter(QuietInjected())
    if otlp_endpoint() is not None:
        install_otlp()
    else:
        path = os.environ.get(SPANS_ENV)
        if not path:
            sys.stderr.write(f"{SPANS_ENV} must name the spans file, or an OTLP endpoint must be set\n")
            raise SystemExit(2)
        install_tracing(path)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
