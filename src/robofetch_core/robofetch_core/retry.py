"""Grab-verification and retry state machine (Complex Functionality 2).

A grab can fail for real reasons - the robot parked too far from the parcel, or the
attach silently did not take hold. FR4 requires the system to retry a failed grab up to
three times before giving up on the order and moving on to the next pending one.

The decision logic lives here, free of ROS, so the whole state machine can be unit-tested
with plain pytest (proposal 5.4) including the "fails on every retry" case.

Transition table - the state machine the proposal asks for:

    state       event            -> next state    action
    ---------------------------------------------------------------------
    NAVIGATING  arrived          -> GRABBING      attempt a grab
    NAVIGATING  nav failed       -> FAILED        give up, next order
    GRABBING    grab ok          -> DELIVERING    carry the parcel to B
    GRABBING    grab failed,     -> RETRYING      back off, re-approach, try again
                attempts < max
    GRABBING    grab failed,     -> FAILED        give up, next order
                attempts = max
    RETRYING    re-approached    -> GRABBING      attempt a grab again
    DELIVERING  arrived          -> RELEASING     put the parcel down
    RELEASING   released         -> COMPLETED

`max_attempts` is 3 by default, matching FR4.
"""
from enum import Enum

from robofetch_core.order import OrderState

DEFAULT_MAX_ATTEMPTS = 3


class GrabDecision(str, Enum):
    """What to do after a grab attempt."""

    PROCEED = "proceed"      # the parcel is held; carry on to the drop-off
    RETRY = "retry"          # back off and try again
    GIVE_UP = "give_up"      # attempts exhausted; fail the order and move on


def decide_after_grab(success, attempts, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Decide what happens after a grab attempt.

    Args:
        success: did the gripper verifiably take hold of the parcel?
        attempts: how many grab attempts have been made INCLUDING this one.
        max_attempts: attempts allowed before the order is failed (FR4: 3).

    Returns:
        GrabDecision.
    """
    if success:
        return GrabDecision.PROCEED
    if attempts < max_attempts:
        return GrabDecision.RETRY
    return GrabDecision.GIVE_UP


def next_state(decision):
    """Map a decision onto the order state it drives."""
    return {
        GrabDecision.PROCEED: OrderState.DELIVERING,
        GrabDecision.RETRY: OrderState.RETRYING,
        GrabDecision.GIVE_UP: OrderState.FAILED,
    }[decision]


def backoff_seconds(attempts, base=2.0):
    """How long to wait before the next attempt.

    Grows with each failure so a transient problem (the parcel still settling, the robot
    still rocking after braking) has progressively more time to clear, without stalling
    the queue for long.
    """
    return base * attempts


def describe(decision, item, attempts, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Human-readable status for the logs, the order history and the client."""
    if decision is GrabDecision.PROCEED:
        return f"grabbed {item} on attempt {attempts}"
    if decision is GrabDecision.RETRY:
        return (f"grab of {item} failed (attempt {attempts} of {max_attempts}); "
                "backing off and re-approaching")
    return f"grab of {item} failed {attempts} times; giving up on this order"
