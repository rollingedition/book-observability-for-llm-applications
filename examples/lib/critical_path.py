"""The critical path of a span tree: the chain of spans that sets a message's end time (plan section 6, chapter 7).

Walk back from the root's end. At each step take the child that ends last at or before the current moment, add it to
the path, and continue from that child's start. A child that overlaps a span already on the path, or that ends after
the moment the walk is at, is not on the path: shortening it does not end the message sooner. Time on the path that no
child covers belongs to the span whose time it is (its self time, the gaps).

Why backwards: the span that ends last sets the parent's end, and whatever ended just before its start sets the time
before that. The walk reads the spans as the replay makes them, so a sequential chain of calls is all on the path.
"""
from collections import defaultdict


def critical_path(rows):
    """Return the span IDs on the critical path of the tree in `rows`, root first and then in time order.

    `rows` are span rows as lib.export.spans_to_rows gives them: `span_id`, `parent_span_id`, `start_ns`, `end_ns` and
    `span_seq`. The tree must have exactly one root, a row whose parent is None. Ties go to the later start, then to the
    lower span_seq, so the same rows always give the same path.
    """
    roots = [row for row in rows if row["parent_span_id"] is None]
    if len(roots) != 1:
        raise ValueError(f"a critical path needs one root, found {len(roots)}")
    children = defaultdict(list)
    for row in rows:
        if row["parent_span_id"] is not None:
            children[row["parent_span_id"]].append(row)
    return _walk(roots[0], children)


def _walk(span, children):
    """The span, then the critical path of each child on its chain, in time order."""
    chain, used, moment = [], set(), span["end_ns"]
    while True:
        # `used` stops a zero-length child from being chosen twice: it ends exactly where its own start is.
        ready = [c for c in children[span["span_id"]] if c["end_ns"] <= moment and c["span_id"] not in used]
        if not ready:
            break
        last = max(ready, key=lambda c: (c["end_ns"], c["start_ns"], -c["span_seq"]))
        chain.append(last)
        used.add(last["span_id"])
        moment = last["start_ns"]
    path = [span["span_id"]]
    for child in reversed(chain):          # the walk went backwards; the path is read forwards
        path.extend(_walk(child, children))
    return path
