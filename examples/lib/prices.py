"""The book's own price table (plan PL-20, section 1.1): what a call would cost at the table's rates, on its virtual time.

`PriceTable()` reads `examples/data/case/prices.toml`, whose version `book-2026-11-02` every printed cost names. `cost`
takes a call's input tokens in total (the cached part included, as the registry counts it, B-71), the cached part and
the output tokens. A model with no row, or a time before the row's start, raises: a price is never borrowed from another
model or another date. The rates are written for the book and are not a quote.
"""
from lib import SEED  # noqa: F401  (importing lib fixes threads and seeds before anything else in this module loads)

import datetime as dt
import tomllib
from decimal import Decimal

from lib.case import CASE_DIR

MILLION = Decimal(1_000_000)               # the table's rates are per million tokens
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def datetime_of(at_ns: int) -> dt.datetime:
    """The UTC moment of a virtual timestamp, as a datetime for genai-prices. Whole microseconds, so no float rounds it."""
    return _EPOCH + dt.timedelta(microseconds=at_ns // 1000)


def _epoch_ns(iso: str) -> int:
    """An ISO 8601 UTC time as integer nanoseconds since the epoch."""
    return (dt.datetime.fromisoformat(iso) - _EPOCH) // dt.timedelta(microseconds=1) * 1000


class PriceTable:
    """The rows of one price table. `version` is the table's own name, printed beside every money figure. A model may
    have several rows, each from its own start date; `row` gives the one in force at a time."""

    def __init__(self, path=None):
        table = tomllib.loads((path or CASE_DIR / "prices.toml").read_text(encoding="utf-8"))
        self.version = table["version"]
        self.currency = table["currency"]
        # Keyed by lower-case name: the recordings name the 4B as qwen3-4b-instruct-2507, the table as Qwen3-4B-Instruct-2507.
        self._rows: dict[str, list[dict]] = {}
        for row in table["model"]:
            self._rows.setdefault(row["model"].lower(), []).append(row)
        for rows in self._rows.values():
            rows.sort(key=lambda r: _epoch_ns(r["valid_from"]))
            starts = [r["valid_from"] for r in rows]
            if len(set(starts)) != len(starts):
                raise ValueError(f"two rows for one model start on the same date: {starts}")

    def row(self, model: str, at_ns: int) -> dict:
        """The row in force for the model at `at_ns`: the latest row that starts at or before it. Otherwise KeyError."""
        rows = self._rows.get(model.lower())
        if not rows:
            raise KeyError(f"no price row for {model!r} in version {self.version}")
        in_force = [r for r in rows if _epoch_ns(r["valid_from"]) <= at_ns]
        if not in_force:
            raise KeyError(f"{model!r} has no price before {rows[0]['valid_from']}")
        return in_force[-1]

    def cost(self, model: str, input_tokens: int, cached_tokens: int, output_tokens: int, at_ns: int) -> Decimal:
        """The money one call costs at the rates in force at `at_ns`, in USD, as an exact Decimal (never rounded here)."""
        if not 0 <= cached_tokens <= input_tokens:
            raise ValueError("cached input tokens are part of the input tokens, so 0 <= cached <= input")
        row = self.row(model, at_ns)
        # A row with cache_rate_source "input rate" bills cached input at the input rate, as genai-prices does for 4B.
        cache_rate = row["input_per_mtok"] if row.get("cache_rate_source") == "input rate" else row["cache_read_per_mtok"]
        uncached = input_tokens - cached_tokens
        money = (Decimal(uncached) * Decimal(row["input_per_mtok"])
                 + Decimal(cached_tokens) * Decimal(cache_rate)
                 + Decimal(output_tokens) * Decimal(row["output_per_mtok"]))
        return money / MILLION

    def cost_of_calls(self, model: str, calls, at_ns: int) -> Decimal:
        """The money of several calls, each a (input, cached, output) triple, at one virtual time. Zero for no calls."""
        total = Decimal(0)
        for input_tokens, cached_tokens, output_tokens in calls:
            total += self.cost(model, input_tokens, cached_tokens, output_tokens, at_ns)
        return total
