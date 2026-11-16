import pytest


def test_small_scheduling_delay_does_not_accumulate_into_lower_frequency():
    from robot_control.scheduling import ControlSchedule
    schedule = ControlSchedule(.01, 1.)
    assert schedule.advance(1.001) == 0
    assert schedule.deadline(1.001) == pytest.approx(1.01)
    assert schedule.advance(1.011) == 0
    assert schedule.deadline(1.011) == pytest.approx(1.02)


def test_missed_periods_are_skipped_and_short_bursts_are_prevented():
    from robot_control.scheduling import ControlSchedule
    schedule = ControlSchedule(.01, 1.)
    assert schedule.advance(1.035) == 3
    assert schedule.deadline(1.) == pytest.approx(1.04)
    assert schedule.deadline(1.035) == pytest.approx(1.043)
    schedule = ControlSchedule(.01, 1.)
    schedule.advance(1.006)
    assert schedule.deadline(1.006) >= 1.014


def test_send_spacing_does_not_move_the_nominal_clock():
    from robot_control.scheduling import ControlSchedule
    schedule = ControlSchedule(.01, 1.)
    schedule.advance(1.006)
    assert schedule.deadline(1.006) == pytest.approx(1.014)
    schedule.advance(1.014)
    assert schedule.nominal == pytest.approx(1.02)
    assert schedule.deadline(1.014) == pytest.approx(1.022)


def test_exact_boundary_and_long_stall_do_not_replay_missed_ticks():
    from robot_control.scheduling import ControlSchedule
    schedule = ControlSchedule(.125, 1.)
    assert schedule.advance(1.25) == 2
    assert schedule.nominal == 1.375
    assert schedule.advance(2.) == 5
    assert schedule.nominal == 2.125
