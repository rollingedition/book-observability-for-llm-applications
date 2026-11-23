"""Trace sampling for chapter 11 (plan section 6, sampling.py): the head decision, the tail rule over whole traces, the
estimate of a total from a sample, and the bytes a trace takes without content.

Head sampling is the SDK's own sampler, `ParentBased(TraceIdRatioBased(ratio))`. The book's IDs count up from 1 (rule 2),
and TraceIdRatioBased reads the low 64 bits of a trace ID, so under sequential IDs every trace would fall under any
ratio. `spread_trace_id` maps each sequential ID to a fixed 128-bit value, the way a random ID would look, and the SDK's
sampler decides on that value. The decision is still deterministic: the same trace gets the same answer on every run.

The replay (`run_day_traced`) builds its provider with no sampler, so the examples apply the head decision to the full
run. A trace's root decides and its spans follow, as ParentBased makes them. `live_provider` samples at the source, for
the one example whose decision has to cross a process, where the contacts server follows it (parent-based sampling).

The tail rule is the book's own code, not the Collector's. It runs over whole traces at the receiver. A trace is complete
when its root arrives, and the root arrives last for this shape (B-83); other shapes would need a wait.
"""
import hashlib
import json
import math
import sys
from pathlib import Path
from dataclasses import dataclass
from decimal import Decimal

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from lib.clock import VirtualClock
from lib.case import load_messages
from lib.prices import PriceTable

STARTUP = "contacts startup"        # the run's one startup trace (lib.dayrun): a trace of the run, not of any message


class SpreadIds(IdGenerator):
    """Trace IDs that look random, and span IDs that count. A live provider samples on these, so the ratio has an effect."""

    def __init__(self):
        self._traces = 0
        self._spans = 0

    def generate_trace_id(self):
        """Return the trace ID for the next trace: the spread value of the next sequential count, from 1."""
        self._traces += 1
        return spread_trace_id(self._traces)

    def generate_span_id(self):
        """Return the next span ID, counting from 1."""
        self._spans += 1
        return self._spans


def spread_trace_id(trace_seq: int, salt: str = "") -> int:
    """The 128-bit trace ID for a sequential one: the first 16 bytes of its SHA-256, read as an integer. A salt makes a
    different hash for the same trace, so repeated draws of one day's head sample are independent.

    Why: TraceIdRatioBased compares the low 64 bits of the trace ID with the ratio. Sequential IDs are small, so they
    all fall under any ratio above zero. A fixed hash gives the sampler the spread a random ID would.
    """
    text = f"trace {trace_seq} {salt}" if salt else f"trace {trace_seq}"
    return int.from_bytes(hashlib.sha256(text.encode("ascii")).digest()[:16], "big")


def head_ratio(ratio) -> ParentBased:
    """The head sampler: the SDK's TraceIdRatioBased for a trace's root, and ParentBased for every span below it, so a
    child follows its parent's decision (chapter 4's sampled flag)."""
    return ParentBased(TraceIdRatioBased(ratio))


def head_keeps(sampler, trace_seq: int, salt: str = "") -> bool:
    """Whether `sampler` keeps the trace with this sequential ID, as the SDK decides for a new root."""
    return sampler.should_sample(None, spread_trace_id(trace_seq, salt), "invoke_agent").decision.is_sampled()


def live_provider(sampler, exporter) -> TracerProvider:
    """A provider that samples at the source with `sampler` and exports each span as it ends (the synchronous processor).

    It is for an example that makes its own spans. The replay's provider does not sample (see the module docstring)."""
    provider = TracerProvider(sampler=sampler, id_generator=SpreadIds(),
                              resource=Resource(attributes={"service.name": "ambrewick-assistant",
                                                            "service.instance.id": "sampling-probe"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


@dataclass(frozen=True)
class Trace:
    """One trace as the receiver holds it: every row with its trace ID, the assistant's and the contacts server's. `seq`
    is the sequential trace ID; `cost` is the book's price for its model calls (chapter 6). A message's trace has its
    `message_id`; a detached root (chapter 4's lost context) matches no arrival, so its `message_id` is empty."""

    seq: int
    message_id: str
    rows: tuple
    duration_ns: int      # the root's end minus its start: the message's time
    error: bool           # any span of the trace has status ERROR (chapter 8's span rule, PL-76)
    tokens: int           # the root's input and output tokens, summed over its model calls (PL-69)
    cost: Decimal         # the model calls' cost at the book's table (chapter 6), each priced at its start


def message_traces(rows, day: str, model: str, price: PriceTable) -> list[Trace]:
    """Group rows into one Trace per trace ID, messages first, in arrival order, then the detached roots. A message's
    root is its `invoke_agent` span, which starts at its message's arrival (lib.gate, PL-76). Any other root is a
    detached root: a trace of its own, judged as one, with no message. Only the contacts startup's trace is left out."""
    arrival_of = {VirtualClock(day, start_ns=m.arrival_ns).now_ns: m.message_id for m in load_messages(day)}
    by_trace = {}
    for row in rows:
        by_trace.setdefault(row["trace_id"], []).append(row)
    traces = []
    for trace_id, members in by_trace.items():
        roots = [r for r in members if r["parent_span_id"] is None]
        assert len(roots) == 1, "every trace the book records has one root span"
        root = roots[0]
        if root["name"] == STARTUP:
            continue
        cost = sum((price.cost(model, r["attributes"].get("gen_ai.usage.input_tokens", 0),
                               r["attributes"].get("gen_ai.usage.cache_read.input_tokens", 0),
                               r["attributes"].get("gen_ai.usage.output_tokens", 0), r["start_ns"])
                    for r in members if r["name"].startswith("chat")), Decimal(0))
        is_message = root["name"].startswith("invoke_agent")
        traces.append(Trace(
            seq=int(trace_id, 16), message_id=arrival_of[root["start_ns"]] if is_message else "",
            rows=tuple(sorted(members, key=lambda r: (r["start_ns"], r["span_id"]))),
            duration_ns=root["end_ns"] - root["start_ns"],
            error=any(r["status"] == "ERROR" for r in members),
            tokens=root["attributes"].get("gen_ai.usage.input_tokens", 0)
            + root["attributes"].get("gen_ai.usage.output_tokens", 0),
            cost=cost))
    return sorted(traces, key=lambda t: (t.message_id == "", int(t.message_id or 0), t.seq))


def only_messages(traces) -> list[Trace]:
    """The traces that belong to a message: the ones every count of messages, and every estimate, is taken over."""
    return [t for t in traces if t.message_id]


def head_sample(traces, ratio, salt: str = "") -> list[Trace]:
    """The traces the head decision keeps at `ratio`, with their rows. A sampled-out trace loses all its rows, the
    contacts server's included, because ParentBased makes each child follow its parent."""
    sampler = head_ratio(ratio)
    return [t for t in traces if head_keeps(sampler, t.seq, salt)]


@dataclass(frozen=True)
class TailRule:
    """The tail rule: keep every trace with an error, every trace slower than `slow_ns`, and every trace costlier than
    `expensive`, within `budget` spans. The three thresholds come from chapters 8, 9 and 6: an error span, the 20 s
    latency objective, and the baseline's 99th percentile of cost.

    `keep(traces)` takes the complete traces and returns {seq: reason}. Traces are taken in priority order, errors
    first, then the slowest, then the costliest; a trace whose spans would overrun the budget is skipped, and a smaller
    one later in the order may still fit. A trace with two reasons is kept once, under the first.
    """

    keep_error: bool
    slow_ns: int
    expensive: Decimal
    budget: int

    def keep(self, traces) -> dict:
        kept, used = {}, 0
        ranked = []
        for t in traces:
            if self.keep_error and t.error:
                ranked.append((0, 0, t.seq, t, "error"))
            elif t.duration_ns > self.slow_ns:
                ranked.append((1, -t.duration_ns, t.seq, t, "slow"))
            elif t.cost > self.expensive:
                ranked.append((2, -t.cost, t.seq, t, "expensive"))
        for _, _, _, t, reason in sorted(ranked, key=lambda item: item[:3]):
            if used + len(t.rows) <= self.budget:
                kept[t.seq] = reason
                used += len(t.rows)
        return kept


def standard_error(values, ratio) -> float:
    """The standard error of the scaled total of a Bernoulli sample at `ratio`: sqrt(sum(y_i^2) * (1 - ratio)) / ratio,
    from the kept values alone. It is zero at ratio 1.0, where every message is kept, and it does not depend on the
    full count."""
    return math.sqrt(sum(float(v) ** 2 for v in values) * (1 - ratio)) / ratio


def estimate_total(values, ratio) -> tuple:
    """The total of a day's values from the kept ones: the sum scaled by 1 / ratio, the Horvitz-Thompson estimate for a
    Bernoulli sample, in which every message has the same chance of being kept (the 1952 paper was not opened). Returns
    (estimate, interval), the interval being the estimate plus or minus 1.96 standard errors (`standard_error`). Its
    width comes from the sample's design, so it is zero at ratio 1.0 and the kept count is free to vary. It is not a
    bootstrap over messages (PL-6 keeps that for the other intervals), and the low end is not clipped at zero."""
    scale = Decimal(str(ratio))
    estimate = Decimal(sum(values)) / scale
    half = Decimal(str(1.96 * standard_error(values, ratio)))
    return estimate, (estimate - half, estimate + half)


def draws_holding(traces, ratio, draws: int, seed: int) -> tuple:
    """How many of `draws` independent head samples of the same traces have an interval that holds the full token count,
    and how many hold the full cost. Draw k salts the trace IDs with the seed and k, so each draw is a fresh sample."""
    full_tokens = sum(t.tokens for t in traces)
    full_cost = sum((t.cost for t in traces), Decimal(0))
    held_tokens = held_cost = 0
    for draw in range(1, draws + 1):
        salted = head_sample(traces, ratio, f"seed {seed} draw {draw}")
        _, (low, high) = estimate_total([t.tokens for t in salted], ratio)
        held_tokens += low <= full_tokens <= high
        _, (low, high) = estimate_total([t.cost for t in salted], ratio)
        held_cost += low <= full_cost <= high
    return held_tokens, held_cost


def retained_bytes(base, traces, kept) -> tuple:
    """Bytes per message on the base day, and the kept traces' bytes against the day's, as (per message, kept, day)."""
    per_message = round(sum(row_bytes(t.rows) for t in base) / len(base))
    return per_message, sum(row_bytes(t.rows) for t in traces if t.seq in kept), sum(row_bytes(t.rows) for t in traces)


# The folders a stack trace names depend on where Python, the packages and the book are installed, so their bytes
# would change from one machine to the next. Each is counted as one short placeholder instead (longest first).
_INSTALL_FOLDERS = sorted({str(Path(__file__).resolve().parents[1]), sys.prefix, sys.base_prefix,
                           sys.exec_prefix, sys.base_exec_prefix}, key=len, reverse=True)


def _without_install_folders(text: str) -> str:
    for folder in _INSTALL_FOLDERS:
        text = text.replace(folder, "<folder>")
    return text


def row_bytes(rows) -> int:
    """The bytes of each span's row in the JSON-lines export (`export.write_jsonl`), newline included. With capture
    off, as in chapter 11, the rows hold no message content; chapter 12 counts what content adds. A stack trace's
    install folders count as one placeholder each, so the count is the same on every machine."""
    return sum(len(_without_install_folders(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)))
               + 1 for row in rows)
