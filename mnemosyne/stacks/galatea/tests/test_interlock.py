"""The interlock is the one piece of Galatea where a bug costs hardware.
Every rule gets a test, including the fail-closed cases."""

import pytest

from app.interlock import ACTIVE_STATES, evaluate_power_off

LIMIT = 50.0


def decide(**kw):
    args = {"reachable": True, "state": "IDLE", "nozzle_c": 25.0, "nozzle_max_c": LIMIT, "force": False}
    args.update(kw)
    return evaluate_power_off(**args)


def test_idle_and_cool_is_allowed():
    d = decide()
    assert d.allowed and d.reason == "safe"


@pytest.mark.parametrize("state", sorted(ACTIVE_STATES))
def test_active_states_block(state):
    d = decide(state=state, nozzle_c=20.0)
    assert not d.allowed and d.reason == "print_active"


def test_unreachable_printer_fails_closed():
    d = decide(reachable=False, state=None, nozzle_c=None)
    assert not d.allowed and d.reason == "printer_unreachable"


def test_unknown_nozzle_fails_closed():
    d = decide(nozzle_c=None)
    assert not d.allowed and d.reason == "nozzle_unknown"


def test_hot_nozzle_blocks_even_when_idle():
    d = decide(state="FINISHED", nozzle_c=180.0)
    assert not d.allowed and d.reason == "hotend_hot"


def test_limit_is_inclusive_on_the_safe_side():
    assert decide(nozzle_c=LIMIT).allowed
    assert not decide(nozzle_c=LIMIT + 0.1).allowed


@pytest.mark.parametrize("kw", [
    {"reachable": False, "state": None, "nozzle_c": None},
    {"state": "PRINTING"},
    {"nozzle_c": 220.0},
])
def test_force_overrides_every_block(kw):
    d = decide(force=True, **kw)
    assert d.allowed and d.reason == "forced"


def test_finished_and_stopped_are_not_active():
    # A finished job leaves the state at FINISHED; only the temperature rule applies.
    for state in ("FINISHED", "STOPPED", "READY", "IDLE"):
        assert decide(state=state).allowed
