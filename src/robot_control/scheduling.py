"""RC tick timing: absorb small wakeup jitter, skip missed periods, no bursts."""
import sys
import math


def prepare_control_threads():
    # RC owns this interpreter. Its socket/cache/serialization threads must
    # yield often enough for the 30 Hz controller and 30 Hz recorder. This
    # bounds the ideal CPython timeslice, not OS scheduling or native calls.
    sys.setswitchinterval(min(sys.getswitchinterval(), .001))


class ControlSchedule:
    """Keep nominal phase independent of the last actual send time."""
    def __init__(self, period, started):
        self.period = period
        self.nominal = started

    def deadline(self, last_send):
        # Preserve the established 20% jitter-correction/no-burst bound.
        return max(self.nominal, last_send + .8*self.period)

    def advance(self, finished):
        periods = max(1, math.floor((finished-self.nominal)/self.period)+1)
        self.nominal += periods*self.period
        # Floating-point rounding at an exact boundary must not replay a tick.
        if self.nominal <= finished:
            self.nominal += self.period
            periods += 1
        return periods-1
