"""One high-resolution monotonic domain for live capture and observation.

On supported Windows Python versions, ``time.monotonic`` can use the coarse
GetTickCount64 clock. Distinct SDK ticks can then receive identical timestamps.
The performance counter uses the monotonic, high-resolution Windows QPC clock.
Keep capture timestamps, freshness observations and deadlines in this domain;
its origin is deliberately unspecified and is not a wall-clock timestamp.
"""

import time


def monotonic_now() -> float:
    """Return unmodified monotonic seconds; never invent timestamp progress."""
    return time.perf_counter()
