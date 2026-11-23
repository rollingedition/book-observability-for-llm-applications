"""A virtual clock. Every span time in the book comes from here, never from the machine (plan rules 1 and 5, section 1.1).

Times are integer nanoseconds since the Unix epoch, in UTC (PL-7, PL-8). A traffic day starts at midnight UTC, so `at`
takes seconds from that midnight. It converts them with decimal arithmetic, not floats, so 0.1 seconds is exactly
100000000 nanoseconds on every machine.
"""
import datetime as dt
from decimal import Decimal

NS_PER_SECOND = 1_000_000_000


class VirtualClock:
    """A clock the caller moves by hand. Nothing here reads the system time."""

    def __init__(self, day, start_ns=0):
        """Start at midnight UTC of `day` (a date, or an ISO date string such as "2026-12-07") plus `start_ns`.

        Why: a day's messages arrive at fixed offsets from its midnight, so the same day always gives the same times.
        """
        if isinstance(day, str):
            day = dt.date.fromisoformat(day)
        midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        self._midnight_ns = int(midnight.timestamp()) * NS_PER_SECOND
        self.now_ns = self._midnight_ns + start_ns

    def at(self, seconds_from_midnight):
        """Return the absolute time, in nanoseconds, that is `seconds_from_midnight` after this day's midnight.

        Why: the seconds arrive as an int or a decimal string from a data file; a float would round them.
        """
        ns = Decimal(str(seconds_from_midnight)) * NS_PER_SECOND
        if ns != ns.to_integral_value():
            raise ValueError(f"{seconds_from_midnight} seconds is not a whole number of nanoseconds")
        return self._midnight_ns + int(ns)

    def advance(self, ns):
        """Move the clock forward by `ns` nanoseconds. A clock never goes back, so a negative step is an error.

        Why: a span that ends before it starts would print a negative duration, which is always a bug in the example.
        """
        if ns < 0:
            raise ValueError("the clock does not go backwards")
        self.now_ns += ns
        return self.now_ns
