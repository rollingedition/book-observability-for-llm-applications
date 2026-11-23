"""Turn finished spans into plain rows, in a stable order, and write them as JSON Lines or OTLP/JSON (plan section 6).

A row is a dict with the span's identity, times in integer nanoseconds, its attributes and its resource. Rows are sorted
by (trace_seq, start_ns, span_seq) (plan rule 4), so the same spans always come out in the same order. Rows keep their
IDs; printing code leaves them out (rule 5). `telemetry.sdk.*` resource values are dropped (rule 6).
"""
import json
from pathlib import Path

_SPAN_KIND_CODE = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
_STATUS_CODE = {"UNSET": 0, "OK": 1, "ERROR": 2}


def _plain(value):
    """Return a value JSON can hold: tuples become lists, and nested values are converted the same way."""
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _sorted_attributes(mapping):
    """Return a dict with its keys sorted, so a dict built in any order prints the same."""
    return {key: _plain(mapping[key]) for key in sorted(mapping)}


def spans_to_rows(exporter):
    """Return one row per finished span the exporter holds, sorted by (trace_seq, start_ns, span_seq).

    `trace_seq` and `span_seq` are the integer IDs. With SequentialIds they count in the order the spans were made.
    The row's `resource` keeps only the service values, not the SDK's telemetry.sdk.* values (rule 6).
    """
    rows = []
    for item in exporter.get_finished_spans():
        context = item.context
        parent = item.parent
        rows.append(
            {
                "trace_seq": context.trace_id,
                "span_seq": context.span_id,
                "trace_id": f"{context.trace_id:032x}",
                "span_id": f"{context.span_id:016x}",
                "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
                "name": item.name,
                "kind": item.kind.name,
                "start_ns": item.start_time,
                "end_ns": item.end_time,
                "status": item.status.status_code.name,
                "attributes": _sorted_attributes(item.attributes or {}),
                "events": [
                    {"name": event.name, "time_ns": event.timestamp, "attributes": _sorted_attributes(event.attributes or {})}
                    for event in item.events
                ],
                "resource": _sorted_attributes(
                    {key: value for key, value in item.resource.attributes.items() if not key.startswith("telemetry.sdk.")}
                ),
                "scope": item.instrumentation_scope.name,
            }
        )
    rows.sort(key=lambda row: (row["trace_seq"], row["start_ns"], row["span_seq"]))
    return rows


def write_jsonl(path, rows):
    """Write one JSON object per line, with sorted keys, so the file is the same bytes on every run."""
    with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")


def read_jsonl(path):
    """Read a file written by write_jsonl and return its rows in the same order."""
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _any_value(value):
    """Encode one attribute value the way OTLP/JSON does: a typed wrapper, with 64-bit integers as strings."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_any_value(item) for item in value]}}
    return {"stringValue": str(value)}


def _key_values(mapping):
    """Encode a dict as OTLP's list of key and value pairs, in key order."""
    return [{"key": key, "value": _any_value(mapping[key])} for key in sorted(mapping)]


def to_otlp_json(rows):
    """Return the rows as one OTLP/JSON trace export request, the shape an OTLP/HTTP receiver accepts.

    Spans are grouped by resource and by instrumentation scope, in the order the rows arrive. Times are strings of
    nanoseconds, since OTLP/JSON carries 64-bit integers as strings.
    """
    groups = {}
    for row in rows:
        resource_key = json.dumps(row["resource"], sort_keys=True)
        groups.setdefault((resource_key, row["scope"]), []).append(row)
    resource_spans = {}
    for (resource_key, scope), members in groups.items():
        entry = resource_spans.setdefault(resource_key, {"resource": {"attributes": _key_values(members[0]["resource"])},
                                                         "scopeSpans": []})
        spans = []
        for row in members:
            encoded = {
                "traceId": row["trace_id"],
                "spanId": row["span_id"],
                "name": row["name"],
                "kind": _SPAN_KIND_CODE[row["kind"]],
                "startTimeUnixNano": str(row["start_ns"]),
                "endTimeUnixNano": str(row["end_ns"]),
                "attributes": _key_values(row["attributes"]),
                "events": [
                    {"timeUnixNano": str(event["time_ns"]), "name": event["name"],
                     "attributes": _key_values(event["attributes"])}
                    for event in row["events"]
                ],
                "status": {"code": _STATUS_CODE[row["status"]]},
            }
            if row["parent_span_id"] is not None:
                encoded["parentSpanId"] = row["parent_span_id"]
            spans.append(encoded)
        entry["scopeSpans"].append({"scope": {"name": scope}, "spans": spans})
    return {"resourceSpans": list(resource_spans.values())}
