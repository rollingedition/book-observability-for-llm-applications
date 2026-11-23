"""The assistant's token metrics (plan section 6, PL-27): the counters and histograms the v0.1.0 registry names for token
usage. Every name and unit comes from `lib.genai.METRICS`, which is generated from the pinned registry; none is typed here.

`TokenMetrics(provider).record_call(...)` adds one model call's tokens to the usage counters (which split by modality,
`gen_ai.token.modality`) and to the per-operation histograms. `totals(reader)` reads the sums back from an in-memory
reader, so an example can compare a metric's total with the spans it came from.
"""
from lib import SEED  # noqa: F401  (importing lib fixes threads and seeds before anything else in this module loads)

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, Sum

from lib.genai import METRICS

USAGE = {                                   # counters: one point per attribute set, summed over the day
    "input": "gen_ai.client.inference.usage.input_tokens",
    "cached": "gen_ai.client.inference.usage.cache_read.input_tokens",
    "output": "gen_ai.client.inference.usage.output_tokens",
}
PER_OPERATION = {                           # histograms: one value per operation (per model call)
    "input": "gen_ai.client.inference.operation.input_tokens",
    "output": "gen_ai.client.inference.operation.output_tokens",
}


def make_meter_provider(reader, *, service_instance_id: str, service_version: str = "1.0.0") -> MeterProvider:
    """A meter provider that exports to `reader`. The resource names the service and its instance explicitly, as every
    resource in the book does (plan rule 12), because the SDK's default instance ID is random."""
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({
        "service.name": "ambrewick-assistant",
        "service.version": service_version,
        "service.instance.id": service_instance_id,
    })
    return MeterProvider(metric_readers=[reader], resource=resource)


def _instrument(make, name: str):
    """Make one instrument with the registry's unit for it. A name the registry does not hold raises."""
    if name not in METRICS:
        raise KeyError(f"{name!r} is not in the pinned registry")
    return make(name, unit=METRICS[name]["unit"])


class TokenMetrics:
    """The token instruments of one meter. Make one per provider, then call `record_call` once per model call."""

    def __init__(self, provider, scope: str = "ambrewick.assistant"):
        meter = provider.get_meter(scope)
        self._counters = {key: _instrument(meter.create_counter, name) for key, name in USAGE.items()}
        self._histograms = {key: _instrument(meter.create_histogram, name) for key, name in PER_OPERATION.items()}

    def record_call(self, *, model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> None:
        """One chat call. `prompt_tokens` is the input total, cached part included (B-71); `cached_tokens` is the cached
        part, derived from the recording (PL-21). A call with no cache adds zero to the cached counter."""
        counted = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": model}
        self._counters["input"].add(prompt_tokens, {**counted, "gen_ai.token.modality": "text"})
        self._counters["cached"].add(cached_tokens, {**counted, "gen_ai.token.modality": "text"})
        self._counters["output"].add(completion_tokens, {**counted, "gen_ai.token.modality": "text"})
        self._histograms["input"].record(prompt_tokens, counted)
        self._histograms["output"].record(completion_tokens, counted)


def totals(reader) -> dict:
    """Read a reader's totals, keyed by the registry's name. A counter gives {"sum"}; a histogram gives {"sum", "count"}.
    Every point of a metric is added, so the totals cover every attribute set the metric has."""
    out = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points = metric.data.data_points
                if isinstance(metric.data, Sum):
                    out[metric.name] = {"sum": sum(p.value for p in points)}
                elif isinstance(metric.data, Histogram):
                    out[metric.name] = {"sum": sum(p.sum for p in points), "count": sum(p.count for p in points)}
    return dict(sorted(out.items()))


# ---- Alerts (chapter 9) ----------------------------------------------------------------------------------------------
# An alert reads the metrics, never the spans. The assistant's three duration histograms take one point per operation,
# recorded at the span's end; an alert reads them as cumulative tallies at each tick, and a window's count is the
# difference between two ticks (per-window counts from the SDK, B-82). A point's attributes are the registry's, never a
# user ID (cardinality, chapter 9). The latency edges are chapter 7's book edges (B-7).
from dataclasses import dataclass

from opentelemetry.sdk.metrics import MeterProvider as _MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from lib.clock import VirtualClock

LATENCY_EDGES_S = (0.5, 1, 2, 5, 10, 20, 30, 60)          # the meter's own upper edges, in seconds: the 20 s objective sits on one (PL-79)
DURATION = {                                              # the operation's span name in the assistant's metrics
    "invoke_agent": "gen_ai.invoke_agent.duration",       # one point per message
    "chat": "gen_ai.client.inference.duration",           # one point per model call attempt
    "execute_tool": "gen_ai.execute_tool.duration",       # one point per tool attempt
}
METRIC_ATTRIBUTES = ("error.type", "gen_ai.agent.name", "gen_ai.operation.name", "gen_ai.prompt.version",
                     "gen_ai.request.model")               # the attributes a metric may carry (cardinality)
NS_PER_S = 1_000_000_000


def make_alert_meter_provider(reader, *, service_instance_id: str, service_version: str = "1.0.0") -> _MeterProvider:
    """`make_meter_provider` with the book's latency edges on each duration histogram. The resource is the same."""
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({
        "service.name": "ambrewick-assistant",
        "service.version": service_version,
        "service.instance.id": service_instance_id,
    })
    views = [View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(LATENCY_EDGES_S))
             for name in DURATION.values()]
    return _MeterProvider(metric_readers=[reader], resource=resource, views=views)


class OperationMetrics:
    """The assistant's duration histograms. The assistant records each operation as it ends, when it is given a meter
    (`Assistant(..., meter_provider=...)`); `record` does the same from a span row (`export.spans_to_rows`)."""

    def __init__(self, provider, scope: str = "ambrewick.assistant"):
        meter = provider.get_meter(scope)
        self._histograms = {op: _instrument(meter.create_histogram, name) for op, name in DURATION.items()}

    def observe(self, operation: str, seconds: float, attributes: dict) -> None:
        """One operation's duration, in seconds, with the metric attributes its attributes hold. Other operations are
        skipped: only the three the registry's duration metrics name are kept."""
        histogram = self._histograms.get(operation)
        if histogram is None:
            return
        kept = {key: attributes[key] for key in METRIC_ATTRIBUTES if key in attributes}
        histogram.record(seconds, kept)

    def record(self, row: dict) -> None:
        """One span row's operation, at the span's end."""
        attributes = row["attributes"]
        self.observe(attributes.get("gen_ai.operation.name"), (row["end_ns"] - row["start_ns"]) / NS_PER_S, attributes)


@dataclass(frozen=True)
class Tally:
    """A cumulative read of one duration histogram: the points counted, those with `error.type`, and those at or under
    the latency edge. Tallies add, so a message's share is the difference between two reads."""

    requests: int
    failed: int
    within: int

    @property
    def slow(self) -> int:
        return self.requests - self.within

    def __add__(self, other: "Tally") -> "Tally":
        return Tally(self.requests + other.requests, self.failed + other.failed, self.within + other.within)

    def __sub__(self, other: "Tally") -> "Tally":
        return Tally(self.requests - other.requests, self.failed - other.failed, self.within - other.within)


def tally(reader, name: str, within_s: float) -> Tally:
    """Read one histogram's cumulative totals. A bucket holds the values up to its upper edge, so the values at or under
    `within_s` are the buckets up to and including that edge."""
    requests = failed = within = 0
    reader.collect()                                   # a read is a collection: the reader holds nothing before one
    data = reader.get_metrics_data()
    for resource in (data.resource_metrics if data is not None else ()):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    requests += point.count
                    if "error.type" in point.attributes:
                        failed += point.count
                    edge = point.explicit_bounds.index(within_s)
                    within += sum(point.bucket_counts[: edge + 1])
    return Tally(requests, failed, within)


class DayMeter:
    """The assistant's live meter for one day, read after each message (`run_day_traced(..., after_message=...)`).

    The assistant records a message's duration at its end, as it runs. `after` reads the message histogram once a message
    has been handled and keeps what that message added: (its end, its tally). The day's ticks are then sums of those
    marks (`tallies_by_tick`), so each tick sees only the messages that ended by then, as an alert would."""

    def __init__(self, day: str, within_s: float = 20):
        self.reader = InMemoryMetricReader()
        self.provider = make_alert_meter_provider(self.reader, service_instance_id=f"{day}-alerts")
        self._within_s = within_s
        self._last = Tally(0, 0, 0)
        self.marks: list[tuple[int, Tally]] = []

    def after(self, run) -> None:
        """Read the histogram after one message (a `Run`): what the message added, keyed by its reply time."""
        now = tally(self.reader, DURATION["invoke_agent"], self._within_s)
        self.marks.append((run.replied_ns, now - self._last))
        self._last = now


def tallies_by_tick(marks: list[tuple[int, Tally]], day: str, step_s: int = 300) -> list[tuple[int, Tally]]:
    """The message histogram's cumulative tally at each tick of the day: every `step_s` seconds from 00:05 to 24:00 UTC.
    A tick holds the messages whose reply came at or before it (the marks from `DayMeter`)."""
    midnight = VirtualClock(day).now_ns
    out = []
    for k in range(1, 86400 // step_s + 1):
        tick = midnight + k * step_s * NS_PER_S
        total = Tally(0, 0, 0)
        for ended, added in marks:
            if ended <= tick:
                total = total + added
        out.append((tick, total))
    return out


def burn_rate(errors: int, requests: int, slo: float) -> float:
    """How fast a window spends the error budget: the share of its requests that failed, over the share the objective
    allows (1 - slo). Burn 1 spends the budget exactly by the window's end; a window with no requests burns nothing."""
    if requests == 0:
        return 0.0
    return (errors / requests) / (1 - slo)


@dataclass(frozen=True)
class Window:
    requests: int
    bad: int
    burn: float


@dataclass(frozen=True)
class Verdict:
    """The alert's reading at one tick: each window's count and burn, and whether the page fires."""

    tick_ns: int
    long: Window
    short: Window
    page: bool


def window_at(cumulative: list[tuple[int, int]], index: int, width: int, slo: float) -> Window:
    """The window of `width` ticks that ends at `index`: the difference of two cumulative reads (B-82)."""
    req_now, bad_now = cumulative[index]
    req_then, bad_then = cumulative[index - width] if index >= width else (0, 0)
    requests, bad = req_now - req_then, bad_now - bad_then
    return Window(requests, bad, burn_rate(bad, requests, slo))


def alert_verdicts(tallies: list[tuple[int, Tally]], *, slo: float, measure: str, long_ticks: int = 12,
                   short_ticks: int = 1, threshold: float = 14.4, min_requests: int = 10) -> list[Verdict]:
    """Evaluate a two-window burn-rate alert at each tick. `measure` is "failed" (a message with `error.type`) or
    "slow" (a message over the latency edge). The page fires when both windows burn at `threshold` or more, and the long
    window holds at least `min_requests` requests, so one failure in a quiet hour cannot page."""
    cumulative = [(t.requests, t.failed if measure == "failed" else t.slow) for _, t in tallies]
    out = []
    for index, (tick, _) in enumerate(tallies):
        long = window_at(cumulative, index, long_ticks, slo)
        short = window_at(cumulative, index, short_ticks, slo)
        page = long.burn >= threshold and short.burn >= threshold and long.requests >= min_requests
        out.append(Verdict(tick, long, short, page))
    return out
