"""Build a tracer provider and record spans with explicit times (plan section 6, tracing.py; rules 1, 3 and 12).

A span takes its start time when it is opened and its end time when it is ended. Both come from the caller, which
takes them from the virtual clock. Nothing here reads a clock, so the same calls give the same spans every run.
"""
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind

from lib.genai import SCHEMA_URL


class BookProvider(TracerProvider):
    """A TracerProvider whose tracers carry the book's schema URL, as its resource does, unless a caller names one."""

    def __init__(self, schema_url, **kwargs):
        super().__init__(**kwargs)
        self._schema_url = schema_url

    def get_tracer(self, instrumenting_module_name, instrumenting_library_version=None, schema_url=None,
                   attributes=None):
        return super().get_tracer(instrumenting_module_name, instrumenting_library_version,
                                  schema_url or self._schema_url, attributes)


def make_provider(service_name, service_version, service_instance_id, clock, ids, exporter, schema_url=SCHEMA_URL):
    """Return a TracerProvider whose spans go to `exporter` as soon as they end.

    `schema_url` defaults to the registry's schema (genai.SCHEMA_URL, plan section 8.1). It goes on the resource and on
    every tracer the provider returns, so a scope's schema matches the resource's. Chapter 1 passes its own 1.44.0.

    Why each part is set this way:
    - The resource is built from the three service values only. Resource(...) does not add the SDK's own
      telemetry.sdk.* values or read OTEL_RESOURCE_ATTRIBUTES, so a reader's environment cannot change the output.
    - service.instance.id is passed in, because the SDK's default is a new random UUID on every run.
    - SimpleSpanProcessor exports synchronously, when each span ends. BatchSpanProcessor would export later, on a
      background thread, in an order that depends on timing (rule 3).
    - `clock` is accepted so every provider in the book is built the same way. The clock is not read here: span times
      are passed to span() by the caller.
    """
    resource = Resource(
        attributes={
            "service.name": service_name,
            "service.version": service_version,
            "service.instance.id": service_instance_id,
        },
        schema_url=schema_url,
    )
    provider = BookProvider(schema_url, id_generator=ids, resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def span(tracer, name, start_ns, end_ns, parent=None, kind=SpanKind.INTERNAL, attributes=None):
    """Record one span from `start_ns` to `end_ns` (integer nanoseconds) and return it, already ended.

    `parent` is a span returned by an earlier call, or None for a new trace. The returned span can be the parent of
    later calls. Why the times go to two calls: start_span takes a start time but no end time, so the end goes to
    span.end(end_time=...) (B-1). An end before the start is refused, since it would print a negative duration.
    """
    if end_ns < start_ns:
        raise ValueError(f"span {name} ends before it starts")
    context = trace.set_span_in_context(parent) if parent is not None else None
    recorded = tracer.start_span(name, context=context, kind=kind, start_time=start_ns, attributes=attributes)
    recorded.end(end_time=end_ns)
    return recorded
