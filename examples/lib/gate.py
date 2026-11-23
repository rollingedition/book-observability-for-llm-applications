"""Chapter 10's release loader and gate rules (plan PL-47c), shared by the chapter's examples.

`measured(folder, day, model)` reads one release's recorded traces and call files into one dict per message: message
time, input tokens, cost, error and route. `paired(base, other)` lines two releases up by message. `judge` applies the
gate's rules to two lists of the same messages (paired) or to two unrelated samples, prints each rule's line and returns
the release's verdict. `paired_report` prints the per-release comparison the latency and figure examples share.
`release_spans` replays the same messages under several releases into one span store, for the query example.

The gate reads the traces and the replayed spans, never an answer. A rule flags a release when the end of its interval nearest the limit lies
past the limit, so a release is worse than the limit at 95 % confidence.
"""
from lib import SEED  # noqa: F401  (importing lib fixes threads and seeds before anything else in this module loads)

import json
from decimal import Decimal
from fractions import Fraction
from functools import partial

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lib.assistant import RELEASES, Assistant, parse_route
from lib.bm25 import Retriever
from lib.case import load_articles, load_messages, load_tool_durations, scenario_to_route
from lib.clock import VirtualClock
from lib.dayrun import DATA
from lib.export import spans_to_rows
from lib.ids import SequentialIds
from lib.prices import PriceTable
from lib.replay import Replay, ReplayContacts
from lib.report import fmt_int, fmt_ms, fmt_share, fmt_usd
from lib.stats import (mean_difference, mean_of, nearest_rank, paired_interval, paired_mean_difference,
                       percentile_difference, unpaired_interval)
from lib.tools import Tools
from lib.tracing import make_provider

PRICES = PriceTable()
P95 = partial(percentile_difference, p=95)     # the p95 of the other release minus the p95 of r0

# The gate's rules. Each reads one number from the traces; none reads an answer. The limit is a tenth of r0's mean for
# the three amounts, and two or five points for the two rates: a rate of zero in r0 leaves no room for a relative limit.
GATE = [
    # label, the message's value, the allowed change, relative to r0's mean?, a regression is a rise (True) or a fall
    ("message time, mean", "time", Fraction(1, 10), True, True),
    ("input tokens per message, mean", "tokens", Fraction(1, 10), True, True),
    ("cost per message, mean", "cost", Fraction(1, 10), True, True),
    ("error rate", "error", Fraction(2, 100), False, True),
    ("route accuracy", "correct", Fraction(5, 100), False, False),
]


# The release that made each recording folder, for release_spans (PL-76).
FOLDER_RELEASE = {"baseline-day-1": "r0", "baseline-day-2": "r0", "release-prompt-v2": "r1",
                  "release-model-swap": "r2", "release-top-8": "r3", "release-cache-off": "r4"}


def error_messages(day: str, folder: str, message_ids: set) -> set:
    """The messages with an error under PL-47c (chapter 8): any span of the message has status ERROR. The release is
    replayed for them; each span is matched to its message by its trace, and a message's root span starts at its
    arrival on the clock."""
    rows = release_spans(day, {FOLDER_RELEASE[folder]: folder}, message_ids)
    arrival_of = {VirtualClock(day, start_ns=m.arrival_ns).now_ns: m.message_id
                  for m in load_messages(day) if m.message_id in message_ids}
    message_of = {row["trace_id"]: arrival_of[row["start_ns"]] for row in rows if row["parent_span_id"] is None}
    return {message_of[row["trace_id"]] for row in rows if row["status"] == "ERROR"}


def measured(folder, day, model):
    """Each message the release recorded, as its trace rows and call files give it: message time, input tokens, cost,
    error and route. Message time is the sum of the recorded durations of its model and tool calls, the time chapter 7
    counts; the recorder's gaps between calls are left out. The error is PL-47c's rule, read from the replayed spans."""
    arrivals = {m.message_id: m for m in load_messages(day)}
    rows = [json.loads(line) for line in (DATA / folder / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    messages = {}
    for row in rows:
        call = json.loads((DATA / folder / "calls" / f"{row['input_key']}.json").read_text(encoding="utf-8"))
        message = messages.setdefault(row["message_id"], {"time": 0, "tokens": 0, "cost": Decimal(0), "error": False,
                                                          "route": "fallback"})
        message["time"] += row["total_ns"]
        if row["kind"] == "mcp":
            continue
        at_ns = VirtualClock(day, start_ns=arrivals[row["message_id"]].arrival_ns).now_ns
        message["tokens"] += row["prompt_tokens"]
        message["cost"] += PRICES.cost(model, row["prompt_tokens"], row["cached_tokens_derived"],
                                       row["completion_tokens"], at_ns)
        if row["step"] == "route":
            message["route"] = parse_route(call["text"]) or "fallback"
    for message_id, message in messages.items():
        message["correct"] = message["route"] == scenario_to_route(arrivals[message_id].scenario)
    for message_id in error_messages(day, folder, set(messages)):
        messages[message_id]["error"] = True
    return messages


def tokens_by_step(folder: str, day: str, model: str) -> dict:
    """Each message's model calls, summed by step: {message_id: {step: {input, output, time, cost, replies}}}. The steps
    are "route" and "answer". A message routed to fallback has no answer entry, because its reply takes no model call.
    `replies` holds each call's text in order, so a reader can check what a call wrote."""
    arrivals = {m.message_id: m for m in load_messages(day)}
    rows = [json.loads(line) for line in (DATA / folder / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = {}
    for row in rows:
        if row["kind"] != "model":
            continue
        call = json.loads((DATA / folder / "calls" / f"{row['input_key']}.json").read_text(encoding="utf-8"))
        at_ns = VirtualClock(day, start_ns=arrivals[row["message_id"]].arrival_ns).now_ns
        step = steps.setdefault(row["message_id"], {}).setdefault(
            row["step"], {"input": 0, "output": 0, "time": 0, "cost": Decimal(0), "replies": []})
        step["input"] += row["prompt_tokens"]
        step["output"] += row["completion_tokens"]
        step["time"] += row["total_ns"]
        step["cost"] += PRICES.cost(model, row["prompt_tokens"], row["cached_tokens_derived"],
                                    row["completion_tokens"], at_ns)
        step["replies"].append(call["text"])
    return steps


def paired(base: dict, other: dict):
    """The messages `other` recorded, in order, with r0's values for the same messages: (ids, base's list, other's list).
    r0 has every one of them."""
    ids = sorted(other)
    return ids, [base[i] for i in ids], [other[i] for i in ids]


def release_spans(day: str, folders: dict, message_ids: set) -> list[dict]:
    """Replay the same messages under each release (name -> its recording folder), in one span store: the spans as rows
    (export.spans_to_rows). One sequence makes the span IDs, so no two releases share an ID."""
    exporter = InMemorySpanExporter()
    ids = SequentialIds()
    messages = load_messages(day)
    for name, folder in folders.items():
        release = RELEASES[name]
        replay = Replay(DATA / folder)   # the release's own recorded calls, answered by input key
        provider = make_provider("ambrewick-assistant", release.service_version, f"{name}-replay",
                                 VirtualClock(day), ids, exporter)
        tracer, retriever = provider.get_tracer("ambrewick.assistant"), Retriever(load_articles(), release.top_k)
        for message in (m for m in messages if m.message_id in message_ids):
            clock = VirtualClock(day, start_ns=message.arrival_ns)
            tools = Tools(load_tool_durations(), ReplayContacts(replay))
            Assistant(replay, retriever, tools, clock, tracer, release).handle(message)
    return spans_to_rows(exporter)


def to_decimal(value):
    """A Fraction as a Decimal, exact, for the report module's formatters."""
    return Decimal(value.numerator) / Decimal(value.denominator)


def signed(value, text):
    """The text with a sign in front, so a fall reads as a fall."""
    return ("-" if value < 0 else "+") + text


def judge(name, before, after, paired):
    """Print the gate's line for each rule and return the release's verdict. `before` and `after` are lists of the same
    messages' values when `paired`, and two samples of different messages when not."""
    flagged = []
    point_of = paired_interval if paired else unpaired_interval
    stat = paired_mean_difference if paired else mean_difference
    for label, key, allowed, relative, rise in GATE:
        b = [m[key] for m in before]
        a = [m[key] for m in after]
        point, (low, high) = point_of(b, a, stat)
        limit = allowed * mean_of(b) if relative else allowed
        if key == "cost":
            shown = [fmt_usd(to_decimal(x * 1000)) for x in (point, limit)]
            print(f"{name}, {label}, per 1,000 messages: {shown[0]}, limit {shown[1]}",
                  f"(95% interval {fmt_usd(to_decimal(low * 1000))} to {fmt_usd(to_decimal(high * 1000))})")
        elif key in ("time", "tokens"):
            unit = fmt_ms if key == "time" else fmt_int
            shown = [unit(round(x)) for x in (point, limit)]
            print(f"{name}, {label}: {shown[0]}, limit {shown[1]}",
                  f"(95% interval {unit(round(low))} to {unit(round(high))})")
        else:
            percent = lambda x: fmt_share(to_decimal(abs(x) * 100), 100)
            print(f"{name}, {label}: {signed(point, percent(point))}, limit {'-' if not rise else '+'}"
                  f"{percent(limit)}",
                  f"(95% interval {signed(low, percent(low))} to {signed(high, percent(high))})")
        breached = low > limit if rise else high < -limit
        if breached:
            flagged.append(label)
        print(f"{name}, {label}, flagged:", "yes" if breached else "no")
    verdict = "fail" if flagged else "pass"
    print(f"{name}, gate verdict:", verdict)
    return verdict, flagged


def paired_report(name, before, after):
    """Print one release's paired comparison with r0 on the same messages: the message time's mean and p95 differences,
    the input tokens' mean difference with its paired and unpaired interval widths, the cost, and the two rates."""
    time_before, time_after = [m["time"] for m in before], [m["time"] for m in after]
    tokens_before, tokens_after = [m["tokens"] for m in before], [m["tokens"] for m in after]
    print(f"{name}, messages (paired with r0): {fmt_int(len(before))}")
    print(f"{name}, r0 p95 message time on these messages:", fmt_ms(nearest_rank(time_before, 95)))
    point, (low, high) = paired_interval(time_before, time_after, paired_mean_difference)
    print(f"{name}, message time, mean difference:", fmt_ms(round(point)),
          f"(95% interval {fmt_ms(round(low))} to {fmt_ms(round(high))})")
    point, (low, high) = paired_interval(time_before, time_after, P95)
    print(f"{name}, message time, p95 difference:", fmt_ms(round(point)),
          f"(95% interval {fmt_ms(round(low))} to {fmt_ms(round(high))})")
    point, (low, high) = paired_interval(tokens_before, tokens_after, paired_mean_difference)
    print(f"{name}, input tokens per message, mean difference:", fmt_int(round(point)),
          f"(95% interval {fmt_int(round(low))} to {fmt_int(round(high))})")
    _, (u_low, u_high) = unpaired_interval(tokens_before, tokens_after, mean_difference)
    paired_width, unpaired_width = round(high) - round(low), round(u_high) - round(u_low)   # from the printed ends
    print(f"{name}, input tokens, interval width paired:", fmt_int(paired_width))
    print(f"{name}, input tokens, interval width if unpaired:", fmt_int(unpaired_width))
    assert paired_width < unpaired_width   # the shared messages narrow the interval
    point, (low, high) = paired_interval([m["cost"] for m in before], [m["cost"] for m in after], paired_mean_difference)
    print(f"{name}, cost per 1,000 messages, mean difference:", fmt_usd(to_decimal(point * 1000)),
          f"(95% interval {fmt_usd(to_decimal(low * 1000))} to {fmt_usd(to_decimal(high * 1000))})")
    for label, key in (("error rate", "error"), ("route accuracy", "correct")):
        point, (low, high) = paired_interval([int(m[key]) for m in before], [int(m[key]) for m in after],
                                             paired_mean_difference)
        share = lambda x: fmt_share(to_decimal(abs(x) * 100), 100)
        print(f"{name}, {label}, mean difference:", signed(point, share(point)),
              f"(95% interval {signed(low, share(low))} to {signed(high, share(high))})")
