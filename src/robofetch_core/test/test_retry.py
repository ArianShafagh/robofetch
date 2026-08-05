"""Unit tests for the grab-retry state machine (proposal 5.4, FR4).

Covers the cases the proposal calls out - a grab that succeeds first time, and one that
fails on every retry - plus the full sequence of states an order passes through, with
grab outcomes mocked so no simulator is needed.

Run with:  pytest src/robofetch_core/test/test_retry.py -v
"""
import pytest

from robofetch_core.order import Order, OrderState
from robofetch_core.retry import (DEFAULT_MAX_ATTEMPTS, GrabDecision, backoff_seconds,
                                  decide_after_grab, describe, next_state)


def make_order():
    return Order(order_id=1, item="item_1", pickup=(0.0, 0.0), dropoff=(1.0, 1.0))


# ------------------------------------------------------------------ the decision rule
def test_successful_grab_proceeds():
    assert decide_after_grab(True, attempts=1) is GrabDecision.PROCEED


def test_success_on_a_later_attempt_still_proceeds():
    assert decide_after_grab(True, attempts=3) is GrabDecision.PROCEED


def test_first_failure_retries():
    assert decide_after_grab(False, attempts=1) is GrabDecision.RETRY


def test_second_failure_retries():
    assert decide_after_grab(False, attempts=2) is GrabDecision.RETRY


def test_third_failure_gives_up():
    """FR4: retry up to 3 times, then fail the order."""
    assert decide_after_grab(False, attempts=3) is GrabDecision.GIVE_UP


def test_attempts_beyond_the_limit_still_give_up():
    assert decide_after_grab(False, attempts=99) is GrabDecision.GIVE_UP


def test_max_attempts_is_configurable():
    assert decide_after_grab(False, attempts=1, max_attempts=1) is GrabDecision.GIVE_UP
    assert decide_after_grab(False, attempts=4, max_attempts=5) is GrabDecision.RETRY


def test_default_matches_the_requirement():
    assert DEFAULT_MAX_ATTEMPTS == 3


# ------------------------------------------------------------------ state mapping
@pytest.mark.parametrize("decision,expected", [
    (GrabDecision.PROCEED, OrderState.DELIVERING),
    (GrabDecision.RETRY, OrderState.RETRYING),
    (GrabDecision.GIVE_UP, OrderState.FAILED),
])
def test_next_state(decision, expected):
    assert next_state(decision) is expected


# ------------------------------------------------------------------ whole sequences
def run_grabs(order, outcomes, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Drive the state machine with a list of mocked grab outcomes.

    Mirrors what the task manager does, so the test exercises the real decision path.
    """
    for success in outcomes:
        order.attempts += 1
        decision = decide_after_grab(success, order.attempts, max_attempts)
        order.set_state(next_state(decision),
                        describe(decision, order.item, order.attempts, max_attempts))
        if decision is not GrabDecision.RETRY:
            return decision
    return GrabDecision.RETRY


def test_grab_succeeds_first_time():
    order = make_order()
    assert run_grabs(order, [True]) is GrabDecision.PROCEED
    assert order.state is OrderState.DELIVERING
    assert order.attempts == 1


def test_grab_fails_once_then_succeeds():
    order = make_order()
    assert run_grabs(order, [False, True]) is GrabDecision.PROCEED
    assert order.state is OrderState.DELIVERING
    assert order.attempts == 2
    # It must have passed through RETRYING on the way.
    assert OrderState.RETRYING.value in [state for state, _ in order.history]


def test_grab_fails_every_time_fails_the_order():
    """The proposal's explicit edge case: a grab that fails on every retry."""
    order = make_order()
    assert run_grabs(order, [False, False, False]) is GrabDecision.GIVE_UP
    assert order.state is OrderState.FAILED
    assert order.attempts == 3
    assert order.finished


def test_never_exceeds_the_attempt_limit():
    order = make_order()
    run_grabs(order, [False] * 10)
    assert order.attempts == DEFAULT_MAX_ATTEMPTS


def test_history_records_every_transition():
    order = make_order()
    run_grabs(order, [False, False, True])
    states = [state for state, _ in order.history]
    assert states == [OrderState.RETRYING.value,
                      OrderState.RETRYING.value,
                      OrderState.DELIVERING.value]


# ------------------------------------------------------------------ backoff
def test_backoff_grows_with_each_attempt():
    assert backoff_seconds(1) < backoff_seconds(2) < backoff_seconds(3)


def test_backoff_is_bounded_for_the_allowed_attempts():
    """The queue must not stall: total backoff stays a few seconds."""
    total = sum(backoff_seconds(a) for a in range(1, DEFAULT_MAX_ATTEMPTS + 1))
    assert total <= 15.0


# ------------------------------------------------------------------ messages
def test_messages_mention_the_item_and_attempt():
    assert "item_1" in describe(GrabDecision.RETRY, "item_1", 2)
    assert "2" in describe(GrabDecision.RETRY, "item_1", 2)
    assert "giving up" in describe(GrabDecision.GIVE_UP, "item_1", 3)
