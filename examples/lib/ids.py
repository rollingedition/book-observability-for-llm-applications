"""Sequential trace and span IDs, so every run of an example makes the same IDs (plan rule 2, section 1.4).

The SDK's default generator draws random IDs, which would change every run. Here the trace IDs count 1, 2, 3 and the
span IDs count 1, 2, 3 on their own. Each example resets them before it starts.
"""
from opentelemetry.sdk.trace.id_generator import IdGenerator


class SequentialIds(IdGenerator):
    """An ID generator that counts. Pass one to make_provider; reset() between examples or runs."""

    def __init__(self):
        """Start both counters at zero, so the first ID handed out is 1."""
        self._trace_count = 0
        self._span_count = 0

    def generate_trace_id(self):
        """Return the next trace ID. Trace IDs must be non-zero, so the count starts at 1."""
        self._trace_count += 1
        return self._trace_count

    def generate_span_id(self):
        """Return the next span ID. Span IDs must be non-zero, so the count starts at 1."""
        self._span_count += 1
        return self._span_count

    def reset(self):
        """Start both counters again, so a second run of the same code makes the same IDs."""
        self._trace_count = 0
        self._span_count = 0
