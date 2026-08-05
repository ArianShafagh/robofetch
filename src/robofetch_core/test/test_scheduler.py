"""Unit tests for the nearest-neighbour scheduler (proposal 5.4).

Covers the edge cases the proposal calls out - an empty queue, a single pending order -
plus tie-breaking, re-evaluation after a delivery, and a check that the greedy order
actually beats naive submission order.

Run with:  pytest src/robofetch_core/test/test_scheduler.py -v
"""
import pytest

from robofetch_core.order import Order, OrderState
from robofetch_core.scheduler import (distance, schedule_all, select_next_order,
                                      total_travel)


def make_order(order_id, pickup, dropoff=(0.0, 0.0), state=OrderState.PENDING):
    return Order(order_id=order_id, item=f"item_{order_id}",
                 pickup=pickup, dropoff=dropoff, state=state)


# --------------------------------------------------------------------- edge cases
def test_empty_queue_returns_none():
    assert select_next_order((0.0, 0.0), []) is None


def test_queue_with_no_pending_orders_returns_none():
    orders = [make_order(1, (1.0, 1.0), state=OrderState.COMPLETED),
              make_order(2, (2.0, 2.0), state=OrderState.FAILED)]
    assert select_next_order((0.0, 0.0), orders) is None


def test_single_pending_order_is_selected():
    only = make_order(1, (5.0, 5.0))
    assert select_next_order((0.0, 0.0), [only]) is only


# ------------------------------------------------------------------ core behaviour
def test_picks_nearest_not_first_submitted():
    """The whole point: submission order must not decide execution order."""
    far = make_order(1, (10.0, 0.0))      # submitted first, but far away
    near = make_order(2, (1.0, 0.0))      # submitted later, but closest
    assert select_next_order((0.0, 0.0), [far, near]) is near


def test_selection_follows_the_robot():
    """Same queue, different robot position -> different choice."""
    left = make_order(1, (-5.0, 0.0))
    right = make_order(2, (5.0, 0.0))
    orders = [left, right]
    assert select_next_order((-4.0, 0.0), orders) is left
    assert select_next_order((4.0, 0.0), orders) is right


def test_completed_orders_are_skipped():
    done = make_order(1, (0.1, 0.0), state=OrderState.COMPLETED)
    pending = make_order(2, (9.0, 0.0))
    assert select_next_order((0.0, 0.0), [done, pending]) is pending


def test_ties_break_on_order_id_deterministically():
    first = make_order(1, (3.0, 0.0))
    second = make_order(2, (0.0, 3.0))     # exactly the same distance
    assert select_next_order((0.0, 0.0), [second, first]) is first


def test_in_progress_order_is_not_reselected():
    running = make_order(1, (0.1, 0.0), state=OrderState.DELIVERING)
    waiting = make_order(2, (8.0, 0.0))
    assert select_next_order((0.0, 0.0), [running, waiting]) is waiting


# ----------------------------------------------------------------- full sequencing
def test_schedule_all_reevaluates_from_each_dropoff():
    """After a delivery the robot is at that drop-off, which changes what is nearest."""
    a = make_order(1, pickup=(1.0, 0.0), dropoff=(2.0, 0.0))
    b = make_order(2, pickup=(2.5, 0.0), dropoff=(3.0, 0.0))
    c = make_order(3, pickup=(10.0, 0.0), dropoff=(11.0, 0.0))
    order_ids = [o.order_id for o in schedule_all((0.0, 0.0), [c, b, a])]
    assert order_ids == [1, 2, 3]


def test_greedy_beats_submission_order():
    """Sanity check that the heuristic actually saves travel."""
    a = make_order(1, pickup=(9.0, 0.0), dropoff=(9.5, 0.0))
    b = make_order(2, pickup=(1.0, 0.0), dropoff=(1.5, 0.0))
    submitted = [a, b]
    greedy = schedule_all((0.0, 0.0), submitted)
    assert total_travel((0.0, 0.0), greedy) < total_travel((0.0, 0.0), submitted)


def test_schedule_all_on_empty_queue():
    assert schedule_all((0.0, 0.0), []) == []


# -------------------------------------------------------------------- distance fn
@pytest.mark.parametrize("a,b,expected", [
    ((0.0, 0.0), (3.0, 4.0), 5.0),
    ((1.0, 1.0), (1.0, 1.0), 0.0),
    ((-2.2, -2.2), (2.2, 2.2), pytest.approx(6.2225, abs=1e-3)),
])
def test_distance(a, b, expected):
    assert distance(a, b) == expected


# ------------------------------------------------------------------------- NFR2
def test_scheduler_meets_nfr2_timing():
    """NFR2: re-evaluate the pending queue in under 100 ms for up to 20 orders."""
    import time
    orders = [make_order(i, (float(i), float(-i))) for i in range(1, 21)]
    start = time.perf_counter()
    for _ in range(100):                      # 100 re-evaluations
        select_next_order((0.0, 0.0), orders)
    elapsed_ms = (time.perf_counter() - start) * 1000.0 / 100.0
    assert elapsed_ms < 100.0, f"one evaluation took {elapsed_ms:.3f} ms"
    print(f"\n  NFR2: {elapsed_ms:.4f} ms per evaluation for 20 orders")
