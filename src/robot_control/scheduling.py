"""RC tick timing: absorb small wakeup jitter, skip missed periods, no bursts."""
import sys


def prepare_control_threads():
    # RC owns this interpreter. Its socket/cache/serialization threads must
    # yield often enough for the 30 Hz controller and 30 Hz recorder. This
    # bounds the ideal CPython timeslice, not OS scheduling or native calls.
    sys.setswitchinterval(min(sys.getswitchinterval(), .001))


def next_deadline(previous, tick_started, last_send, period):
    if tick_started - previous >= period:
        return max(tick_started, last_send) + period
    # Keep the nominal clock rather than adding processing/wakeup latency on
    # every cycle. Limit jitter correction to 20% of a period (the established
    # controller no-burst bound); never replay missed ticks.
    return max(previous + period, last_send + .8 * period)
