"""Unit tests for order admission (proposal C2, §11.2).

This is the complex functionality the proposal defends as *ours* rather than a third-party ML
call, so these tests are mostly about the relationship between the policy and the model:

  * the cheapest gate (stock) must run before the expensive ones;
  * the robot must refuse work it could not get home from - the reserve gate;
  * the classifier must never be able to approve one the physical gates rejected;
  * losing the AI must degrade the system to the deterministic rule, not break it (NFR2).

No database, no HTTP, no ROS - `decide()` takes plain dicts, which is what makes the whole
decision path testable without a simulator.
"""
from robofetch_bridge import admission
from robofetch_core.robot_model import RESERVE_PERCENT

STATION = {"station_id": "station_1", "x": 0.0, "y": -2.2}
DELIVERY = {"delivery_id": "delivery_1", "name": "South-west bay", "x": -3.0, "y": -2.2}


def product(weight=1.0, stock=1, pick=(2.75, -1.5)):
    return {"product_id": "SKU-TEST", "name": "Test part", "category": "test",
            "weight_kg": weight, "stock": stock, "shelf_id": "shelf_3",
            "pick_point_id": "pick_3a", "model_name": "parcel_5",
            "pick_x": pick[0], "pick_y": pick[1]}


def state(battery=100.0, temperature=22.0, condition=100.0):
    return {"battery_percent": battery, "temperature_c": temperature,
            "condition_percent": condition}


def yes(_features):
    return True, 0.95, "model"


def no(_features):
    return False, 0.91, "model"


def unavailable(_features):
    return None            # what Predictor returns when the AI cannot be reached


# ----------------------------------------------------------------------- gates


def test_out_of_stock_is_refused_first():
    decision, reason, _, _ = admission.decide(
        product(stock=0), DELIVERY, STATION, state())
    assert decision == admission.REFUSED
    assert "out of stock" in reason


def test_refuses_when_it_could_not_get_back_to_the_station():
    decision, reason, _, cost = admission.decide(
        product(weight=4.5), DELIVERY, STATION, state(battery=20.0))
    assert decision == admission.REFUSED
    assert "reserve" in reason
    assert cost["battery_after_percent"] < RESERVE_PERCENT


# ------------------------------------------------- the model inside the policy


def test_classifier_cannot_rescue_an_order_the_policy_rejected():
    """The point of the gate ordering: physics outranks the model, never the other way round."""
    decision, reason, decided_by, _ = admission.decide(
        product(weight=4.5), DELIVERY, STATION, state(battery=18.0), predictor=yes)
    assert decision == admission.REFUSED
    assert "reserve" in reason
    assert decided_by == "policy"        # the model was never even consulted


def test_system_still_decides_when_the_ai_is_unreachable():
    """NFR2: losing the AI must degrade to the deterministic rule, not block."""
    decision, _, decided_by, _ = admission.decide(
        product(), DELIVERY, STATION, state(), predictor=unavailable)
    assert decision == admission.ACCEPTED
    assert decided_by == "policy"

    refused, reason, _, _ = admission.decide(
        product(weight=4.5), DELIVERY, STATION, state(battery=18.0), predictor=unavailable)
    assert refused == admission.REFUSED
    assert "reserve" in reason
