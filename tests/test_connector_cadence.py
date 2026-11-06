import pytest


def test_small_scheduling_delay_does_not_accumulate_into_lower_frequency():
    from robot_control.scheduling import next_deadline
    assert next_deadline(1., 1.001, 1.001, .01) == pytest.approx(1.01)
    assert next_deadline(1.01, 1.011, 1.011, .01) == pytest.approx(1.02)


def test_missed_periods_are_skipped_and_short_bursts_are_prevented():
    from robot_control.scheduling import next_deadline
    assert next_deadline(1., 1.035, 1.035, .01) == pytest.approx(1.045)
    assert next_deadline(1., 1.006, 1.006, .01) >= 1.014
