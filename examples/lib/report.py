"""The printing rules for the book's numbers (plan section 1.1, PL-8, PL-9). This is the only module that formats
durations, money, counts and shares. Every function returns ASCII text.

Durations are milliseconds with one decimal place. Money is USD with six decimal places and thousands separators.
Counts are integers with thousands separators. Shares are percentages with one decimal place. Burn rates (chapter 9)
are one decimal place, with no unit: the caller names the window they are over.
"""
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

_MONEY_PLACE = Decimal("0.000001")
_SHARE_PLACE = Decimal("0.1")
_BURN_PLACE = Decimal("0.1")


def fmt_ms(ns):
    """Format integer nanoseconds as milliseconds with one decimal place, e.g. 1234567 becomes "1.2 ms".

    Why integers: the rounding is done on whole nanoseconds, half up, so no float rounding can change the last digit.
    """
    if isinstance(ns, bool) or not isinstance(ns, int):
        raise TypeError("durations are integer nanoseconds")
    sign = "-" if ns < 0 else ""
    tenths = (abs(ns) + 50_000) // 100_000
    return f"{sign}{tenths // 10:,d}.{tenths % 10} ms"


def fmt_usd(value):
    """Format a Decimal amount of US dollars with six decimal places, e.g. Decimal("0.000123") becomes "$0.000123".

    Why Decimal: money is never held as a float. The value is rounded only here, for printing; storage keeps every digit.
    """
    if not isinstance(value, Decimal):
        raise TypeError("money is a Decimal")
    rounded = value.quantize(_MONEY_PLACE, rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    return f"{sign}${abs(rounded):,.6f}"


def fmt_int(n):
    """Format an integer with thousands separators, e.g. 1234 becomes "1,234". Floats are refused: a count is whole."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("counts are integers")
    return f"{n:,d}"


def fmt_share(part, whole):
    """Format part of whole as a percentage with one decimal place, e.g. (1, 8) becomes "12.5%".

    Why: a share with no whole to divide by has no meaning, so an empty whole is an error, not "0.0%".
    """
    if whole == 0:
        raise ValueError("a share needs a non-zero whole")
    percent = Decimal(part) * 100 / Decimal(whole)
    return f"{percent.quantize(_SHARE_PLACE, rounding=ROUND_HALF_UP)}%"


def fmt_burn(rate):
    """Format a burn rate with one decimal place, e.g. 14.4 becomes "14.4" and 73.25 becomes "73.2".

    Why this rounding: the exact binary value of the rate is rounded half to even, which is the rounding Python's own
    one-decimal format uses, so a rate prints as it did before the rule was written down. A burn is a rate, so the
    caller names its window beside it.
    """
    if isinstance(rate, bool) or not isinstance(rate, (int, float, Decimal)):
        raise TypeError("a burn rate is a number")
    return str(Decimal(rate).quantize(_BURN_PLACE, rounding=ROUND_HALF_EVEN))  # Decimal(float) is the exact binary value


def fmt_rate(count, denominator):
    """Format a count per a stated number of things, e.g. (3, (1000, "messages")) becomes "3 per 1,000 messages".

    `denominator` is a pair: the number of things, and the name of those things. The name is always printed, so a rate
    never reads as a bare number.
    """
    number, name = denominator
    return f"{fmt_int(count)} per {fmt_int(number)} {name}"
