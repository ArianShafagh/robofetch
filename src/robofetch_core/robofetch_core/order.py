"""Order model and execution states.

Deliberately free of ROS imports so the scheduling logic (M5) and the retry state
machine (M6) can be unit-tested with plain pytest, as the proposal's testing section
requires.
"""
from dataclasses import dataclass, field
from enum import Enum


class OrderState(str, Enum):
    """States an order moves through while being executed.

    Mirrors the state set in the proposal: {navigating, grabbing, retrying,
    delivering, failed, completed}. `PENDING` is the queued-but-not-started state.
    """

    PENDING = "pending"
    NAVIGATING = "navigating"      # driving to the pickup point (A)
    GRABBING = "grabbing"          # attempting to pick the item up
    RETRYING = "retrying"          # a grab failed; backing off before another attempt
    DELIVERING = "delivering"      # carrying the item to the drop-off point (B)
    RELEASING = "releasing"        # putting the item down at B
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATES = (OrderState.COMPLETED, OrderState.FAILED)


@dataclass
class Order:
    """A single delivery request: bring `item` from `pickup` to `dropoff`."""

    order_id: int
    item: str
    pickup: tuple           # (x, y) in map/world coordinates
    dropoff: tuple          # (x, y)
    state: OrderState = OrderState.PENDING
    attempts: int = 0       # grab attempts made so far
    detail: str = ""        # last status message, surfaced to the client
    history: list = field(default_factory=list)   # (state, detail) transitions

    def set_state(self, state: OrderState, detail: str = ""):
        """Record a transition. Every change is logged, per the maintenance section."""
        self.state = state
        self.detail = detail
        self.history.append((state.value, detail))

    @property
    def finished(self):
        return self.state in TERMINAL_STATES
