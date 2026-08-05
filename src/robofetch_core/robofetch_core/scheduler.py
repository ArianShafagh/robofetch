"""Nearest-neighbour order scheduler (Complex Functionality 1).

When several orders are pending, serving them in submission order wastes travel. This
module instead picks, at every decision point, the pending order whose pickup point is
closest to the robot's current position - a greedy nearest-neighbour heuristic, and a
live approximation of the Vehicle Routing Problem.

It is re-evaluated whenever the robot becomes idle or a new order arrives (UC3), so the
plan adapts to where the robot actually ended up rather than being fixed in advance.

Deliberately free of ROS imports so it can be unit-tested directly (proposal 5.4).
"""
import math

from robofetch_core.order import Order, OrderState


def distance(a, b):
    """Straight-line distance between two (x, y) points.

    Euclidean rather than true path length: computing real path costs would mean asking
    Nav2 to plan for every pending order on every decision, which the proposal's NFR2
    budget (under 100 ms for 20 orders) does not allow. In an open maze the straight-line
    distance orders candidates well enough for a greedy choice.
    """
    return math.hypot(a[0] - b[0], a[1] - b[1])


def pending_orders(orders):
    """The orders still waiting to be executed, in submission order."""
    return [o for o in orders if o.state == OrderState.PENDING]


def select_next_order(robot_position, orders):
    """Pick the pending order whose pickup point is nearest the robot.

    Args:
        robot_position: (x, y) the robot is at now.
        orders: every known order; non-pending ones are ignored.

    Returns:
        The closest pending Order, or None if none are pending.

    Ties are broken by order_id, so the outcome is deterministic and the earlier
    submission wins - which keeps the behaviour predictable for the client and for tests.
    """
    candidates = pending_orders(orders)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda order: (distance(robot_position, order.pickup), order.order_id),
    )


def schedule_all(robot_position, orders):
    """Return the full visiting sequence the greedy heuristic would produce.

    Used for analysis, tests and explaining the plan to the client; the live system calls
    `select_next_order` again after each delivery so it can react to new orders.
    Assumes the robot ends each order at that order's drop-off point.
    """
    remaining = list(pending_orders(orders))
    position = robot_position
    sequence = []
    while remaining:
        nxt = min(remaining,
                  key=lambda o: (distance(position, o.pickup), o.order_id))
        sequence.append(nxt)
        remaining.remove(nxt)
        position = nxt.dropoff
    return sequence


def total_travel(robot_position, sequence):
    """Rough total distance for a visiting sequence: robot->A1->B1->A2->B2->...

    Lets us show that the greedy order beats naive submission order.
    """
    total = 0.0
    position = robot_position
    for order in sequence:
        total += distance(position, order.pickup)
        total += distance(order.pickup, order.dropoff)
        position = order.dropoff
    return total
