import pytest

from tonearm.gate import Action, Gate, GateConfig, State

LOUD = -30.0
MID = -67.0
UP = -100.0
CFG = GateConfig(start_db=-60.0, floor_db=-75.0, quiet_timeout_s=180.0, start_hold_s=1.0)


def run(gate, level, start, end, step=0.05):
    actions = []
    for i in range(int(round((end - start) / step)) + 1):
        actions += gate.feed(level, start + i * step)
    return actions


def playing_gate():
    g = Gate(CFG)
    run(g, LOUD, 0.0, 1.0)
    assert g.state is State.PLAYING
    return g


def test_initial_state_follows_auto_on():
    assert Gate(CFG, auto_on=True).state is State.ARMED
    assert Gate(CFG, auto_on=False).state is State.OFF


def test_config_rejects_start_not_above_floor():
    with pytest.raises(ValueError):
        GateConfig(start_db=-80.0, floor_db=-70.0)


def test_needle_drop_starts_after_hold():
    g = Gate(CFG)
    assert run(g, LOUD, 0.0, 0.95) == []
    assert g.state is State.ARMED
    assert g.feed(LOUD, 1.0) == [Action.START_AUTO]
    assert g.state is State.PLAYING


def test_short_bump_does_not_start():
    g = Gate(CFG)
    assert run(g, LOUD, 0.0, 0.5) == []
    assert run(g, UP, 0.55, 5.0) == []
    assert g.state is State.ARMED


def test_hysteresis_keeps_down_reading_between_thresholds():
    g = Gate(CFG)
    run(g, LOUD, 0.0, 0.5)
    assert run(g, MID, 0.55, 1.0) == [Action.START_AUTO]
    assert g.stylus_down is True


def test_mid_level_does_not_count_as_down_when_up():
    g = Gate(CFG)
    assert run(g, MID, 0.0, 10.0) == []
    assert g.stylus_down is False


def test_quiet_passage_never_stops_playback():
    g = playing_gate()
    assert run(g, MID, 1.05, 400.05, step=1.0) == []
    assert g.state is State.PLAYING


def test_arm_up_for_timeout_stops_and_rearms():
    g = playing_gate()
    assert run(g, UP, 2.0, 181.0, step=1.0) == []
    assert g.feed(UP, 182.0) == [Action.STOP]
    assert g.state is State.ARMED


def test_side_flip_within_timeout_keeps_playing():
    g = playing_gate()
    run(g, UP, 2.0, 62.0, step=1.0)
    run(g, LOUD, 63.0, 300.0, step=1.0)
    assert g.state is State.PLAYING
    assert run(g, UP, 301.0, 480.0, step=1.0) == []
    assert g.feed(UP, 481.0) == [Action.STOP]


def test_stop_holds_until_record_ends():
    g = playing_gate()
    assert g.stop(5.0) == [Action.STOP]
    assert g.state is State.HELD
    assert run(g, LOUD, 5.0, 600.0, step=1.0) == []
    assert g.state is State.HELD
    assert run(g, UP, 601.0, 780.0, step=1.0) == []
    assert g.feed(UP, 781.0) == []
    assert g.state is State.ARMED


def test_stop_when_not_playing_is_noop():
    g = Gate(CFG)
    assert g.stop(0.0) == []
    assert g.state is State.ARMED


def test_play_from_any_non_playing_state():
    for auto_on in (True, False):
        g = Gate(CFG, auto_on=auto_on)
        assert g.play(0.0) == [Action.START_MANUAL]
        assert g.state is State.PLAYING
        assert g.play(1.0) == []
    g = playing_gate()
    g.stop(2.0)
    assert g.play(3.0) == [Action.START_MANUAL]


def test_manual_play_with_arm_up_gets_full_timeout():
    g = Gate(CFG)
    run(g, UP, 0.0, 1000.0, step=1.0)
    assert g.play(1000.0) == [Action.START_MANUAL]
    assert run(g, UP, 1001.0, 1179.0, step=1.0) == []
    assert g.feed(UP, 1180.0) == [Action.STOP]


def test_auto_on_off_blocks_auto_start():
    g = Gate(CFG, auto_on=False)
    assert run(g, LOUD, 0.0, 10.0) == []
    assert g.state is State.OFF


def test_disabling_auto_on_does_not_stop_playback():
    g = playing_gate()
    g.set_auto_on(False, 2.0)
    assert g.state is State.PLAYING
    run(g, UP, 3.0, 182.0, step=1.0)
    assert g.feed(UP, 183.0) == [Action.STOP]
    assert g.state is State.OFF


def test_disabling_auto_on_while_held_ends_in_off():
    g = playing_gate()
    g.stop(2.0)
    g.set_auto_on(False, 3.0)
    run(g, UP, 4.0, 184.0, step=1.0)
    assert g.state is State.OFF


def test_toggling_auto_on_between_armed_and_off():
    g = Gate(CFG)
    g.set_auto_on(False, 0.0)
    assert g.state is State.OFF
    g.set_auto_on(True, 1.0)
    assert g.state is State.ARMED


def test_enabling_auto_on_mid_record_starts_after_hold():
    g = Gate(CFG, auto_on=False)
    run(g, LOUD, 0.0, 10.0)
    g.set_auto_on(True, 10.0)
    assert g.feed(LOUD, 10.5) == []
    assert g.feed(LOUD, 11.0) == [Action.START_AUTO]


def test_countdown():
    g = playing_gate()
    assert g.countdown(1.5) is None
    run(g, UP, 2.0, 32.0, step=1.0)
    assert g.countdown(32.0) == pytest.approx(150.0)
    assert Gate(CFG).countdown(0.0) is None


def test_tick_advances_timers_without_new_levels():
    g = playing_gate()
    g.feed(UP, 2.0)
    assert g.tick(181.0) == []
    assert g.tick(182.0) == [Action.STOP]
