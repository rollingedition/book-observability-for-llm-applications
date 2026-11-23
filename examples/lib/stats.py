"""Statistics for the book's printed numbers (plan PL-5, PL-6, section 6).

Percentiles are nearest-rank: the value at sorted position ceil(p/100 * n), 1-based. Intervals are a percentile bootstrap
that resamples messages, 2,000 resamples at 95 %, with a seeded numpy Generator. The interval's end points are themselves
nearest-rank values, so every printed number is one that occurred or one drawn from a fixed seed.
"""
import math
from bisect import bisect_left
from decimal import Decimal
from fractions import Fraction

import numpy as np


def nearest_rank(values, p):
    """Return the p-th percentile of `values` by the nearest-rank rule: the value at position ceil(p/100 * n).

    Why this rule: it is exact and reproducible across numpy versions, and it always returns a value that occurred. It
    is the only percentile the book prints (K-7). p is a number from above 0 to 100, read as a decimal string so that
    99.9 is exactly 99.9.
    """
    if not values:
        raise ValueError("a percentile needs at least one value")
    if not 0 < Fraction(str(p)) <= 100:
        raise ValueError("p must be above 0 and at most 100")
    ordered = sorted(values)
    rank = math.ceil(Fraction(str(p)) * len(ordered) / 100)
    return ordered[rank - 1]


def bootstrap_interval(units, stat, n=2000, seed=20261207, level=0.95):
    """Return (low, high), a percentile bootstrap interval for stat(units), resampling the units with replacement.

    `units` are the per-message values (the resampling unit is the message, never the span: PL-6). `stat` takes a list
    of units and returns a number. Each of the `n` resamples draws len(units) units from a Generator seeded with `seed`,
    so the interval is the same on every run. The ends are the nearest-rank values at the two tails of the resamples.
    """
    if not units:
        raise ValueError("an interval needs at least one unit")
    generator = np.random.Generator(np.random.PCG64(seed))
    population = np.asarray(units, dtype=object)
    estimates = []
    for _ in range(n):
        picks = generator.integers(0, len(units), size=len(units))
        estimates.append(stat(population[picks].tolist()))
    tail = (Decimal("1") - Decimal(str(level))) * 50
    return nearest_rank(estimates, tail), nearest_rank(estimates, 100 - tail)


def paired_difference(base, other):
    """Return other[i] - base[i] for each message, in order. The two lists must hold the same messages in the same order.

    Why: a paired comparison resamples these per-message differences, not the two groups separately (PL-6), so the
    messages' own variation cancels out of the interval.
    """
    if len(base) != len(other):
        raise ValueError("paired lists must be the same length")
    return [after - before for before, after in zip(base, other)]


def bucket_counts(values, bounds):
    """Count values into buckets with the given upper bounds, and return len(bounds) + 1 counts.

    Bucket 0 holds values up to and including bounds[0]; bucket i holds values above bounds[i-1] up to and including
    bounds[i]; the last bucket holds everything above the last bound. The bounds must rise strictly.
    """
    bounds = list(bounds)
    if any(later <= earlier for earlier, later in zip(bounds, bounds[1:])):
        raise ValueError("bounds must rise strictly")
    counts = [0] * (len(bounds) + 1)
    for value in values:
        counts[bisect_left(bounds, value)] += 1
    return counts


def mean_of(values):
    """Return the mean of `values` as an exact Fraction. The values are integers or Decimals, never floats.

    Why exact: a paired difference is the difference of two means, and in a bootstrap it is taken 2,000 times; a float
    sum could round differently from one resample to the next, and a printed interval could then move in its last digit.
    """
    if not values:
        raise ValueError("a mean needs at least one value")
    return Fraction(sum(values)) / len(values)


def paired_mean_difference(base, other):
    """Return the mean of the per-message differences other[i] - base[i], exact (PL-6: the mean of differences)."""
    return mean_of(paired_difference(base, other))


def mean_difference(base, other):
    """Return the mean of `other` minus the mean of `base`, for two samples that share no message."""
    return mean_of(other) - mean_of(base)


def percentile_difference(base, other, p):
    """Return other's nearest-rank p-th percentile minus base's. Within a paired interval the two are resampled jointly."""
    return Fraction(nearest_rank(other, p)) - Fraction(nearest_rank(base, p))


def _interval_ends(estimates, level):
    """The two nearest-rank values that cut off the outer tails of the resampled estimates."""
    tail = (Decimal("1") - Decimal(str(level))) * 50
    return nearest_rank(estimates, tail), nearest_rank(estimates, 100 - tail)


def paired_interval(base, other, stat, n=2000, seed=20261207, level=0.95):
    """Return (point, (low, high)) for stat(base, other) on messages measured under two releases on the same messages.

    `base[i]` and `other[i]` are the same message. Each of the `n` resamples draws message indices once and takes both
    releases' values for them, so the messages' own variation cancels out of the difference (PL-6). `stat` takes the two
    lists and returns the difference. The interval is seeded, so the same data give the same numbers on any machine.
    """
    if len(base) != len(other):
        raise ValueError("paired lists must be the same length")
    if not base:
        raise ValueError("an interval needs at least one message")
    generator = np.random.Generator(np.random.PCG64(seed))
    estimates = []
    for _ in range(n):
        picks = generator.integers(0, len(base), size=len(base)).tolist()
        estimates.append(stat([base[i] for i in picks], [other[i] for i in picks]))
    return stat(base, other), _interval_ends(estimates, level)


def unpaired_interval(base, other, stat, n=2000, seed=20261207, level=0.95):
    """Return (point, (low, high)) for stat(base, other) when the two samples are different messages.

    Each resample draws from each sample on its own, so the two samples' sizes can differ. Use this only when no message
    is shared, such as a second traffic day; a shared message calls for `paired_interval`, which is narrower.
    """
    if not base or not other:
        raise ValueError("an interval needs at least one message in each sample")
    generator = np.random.Generator(np.random.PCG64(seed))
    estimates = []
    for _ in range(n):
        left = generator.integers(0, len(base), size=len(base)).tolist()
        right = generator.integers(0, len(other), size=len(other)).tolist()
        estimates.append(stat([base[i] for i in left], [other[i] for i in right]))
    return stat(base, other), _interval_ends(estimates, level)
