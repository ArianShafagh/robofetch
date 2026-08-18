"""Unit tests for order admission (proposal C2, §11.2).

This is the complex functionality the proposal defends as *ours* rather than a third-party ML
call, so these tests are mostly about the relationship between the policy and the model:

  * the physical gates must be able to refuse an order the classifier approved;
  * the classifier must never be able to approve one the physical gates rejected;
  * losing the AI must degrade the system to the deterministic rule, not break it (NFR2).

No database, no HTTP, no ROS - `decide()` takes plain dicts, which is what makes the whole
decision path testable without a simulator.
"""
import pytest

from robofetch_bridge import admission
from robofetch_core.robot_model import CONDITION_MIN, RESERVE_PERCENT

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


# ------------------------------------------------------------------- estimating
def test_estimate_costs_the_return_leg_too():
    cost = admission.estimate(product(), DELIVERY, STATION, state())
    assert cost["return_distance_m"] > 0
    assert cost["total_distance_m"] > cost["distance_m"]


def test_heavier_products_cost_more_and_run_hotter():
    light = admission.estimate(product(weight=0.3), DELIVERY, STATION, state())
    heavy = admission.estimate(product(weight=4.5), DELIVERY, STATION, state())
    assert heavy["energy_wh"] > light["energy_wh"]
    assert heavy["peak_temperature_c"] > light["peak_temperature_c"]


def test_estimate_measures_from_the_robot_when_its_position_is_known():
    near = admission.estimate(product(), DELIVERY, STATION, state(), robot_xy=(2.75, -1.5))
    far = admission.estimate(product(), DELIVERY, STATION, state(), robot_xy=(-3.5, 2.5))
    assert far["distance_m"] > near["distance_m"]


# ----------------------------------------------------------------------- gates
def test_healthy_robot_accepts_a_normal_order():
    decision, reason, _, _ = admission.decide(product(), DELIVERY, STATION, state())
    assert decision == admission.ACCEPTED
    assert "Wh" in reason


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


def test_refuses_when_the_job_would_overheat_it():
    decision, reason, _, _ = admission.decide(
        product(weight=4.5), DELIVERY, STATION, state(temperature=66.0))
    assert decision == admission.REFUSED
    assert "cool down" in reason


def test_refuses_a_worn_robot():
    decision, reason, _, _ = admission.decide(
        product(), DELIVERY, STATION, state(condition=CONDITION_MIN - 5))
    assert decision == admission.REFUSED
    assert "maintenance" in reason


# ------------------------------------------------- the model inside the policy
def test_classifier_can_refuse_an_order_the_policy_allowed():
    decision, reason, decided_by, _ = admission.decide(
        product(), DELIVERY, STATION, state(), predictor=no)
    assert decision == admission.REFUSED
    assert decided_by == "model"
    assert "predicted infeasible" in reason


def test_classifier_cannot_rescue_an_order_the_policy_rejected():
    """The point of the gate ordering: physics outranks the model, never the other way round."""
    decision, reason, decided_by, _ = admission.decide(
        product(weight=4.5), DELIVERY, STATION, state(battery=18.0), predictor=yes)
    assert decision == admission.REFUSED
    assert "reserve" in reason
    assert decided_by == "policy"        # the model was never even consulted


def test_agreeing_classifier_accepts_and_is_recorded_as_the_decider():
    decision, _, decided_by, _ = admission.decide(
        product(), DELIVERY, STATION, state(), predictor=yes)
    assert decision == admission.ACCEPTED
    assert decided_by == "model"


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


# ------------------------------------------------------- the reservation ledger
# The gates above judge one order against a robot standing still. These check the harder case:
# an order arriving while the robot is already committed to work it has not finished.
def test_projection_is_a_no_op_when_nothing_is_committed():
    live = state(battery=80.0)
    assert admission.project(live, None) == live
    assert admission.project(live, {"orders": 0, "energy_wh": 0.0,
                                    "peak_temperature_c": None}) == live


def test_a_reservation_refuses_an_order_the_live_battery_would_have_allowed():
    """Two orders judged against the same battery can together exceed the pack."""
    alone = admission.decide(product(), DELIVERY, STATION, state(battery=60.0))
    assert alone[0] == admission.ACCEPTED

    # Same robot, same order - but work already accepted has spoken for most of the charge.
    queued = admission.decide(product(), DELIVERY, STATION, state(battery=60.0),
                              committed={"orders": 2, "energy_wh": 9.5,
                                         "peak_temperature_c": None})
    assert queued[0] == admission.REFUSED
    assert "reserve" in queued[1]
    # The refusal has to explain itself, or it contradicts the battery on the robot page.
    assert "already in progress" in queued[1]


def test_a_reservation_carries_the_hottest_committed_job_forward():
    """Energy sums across queued orders; heat takes the maximum instead."""
    alone = admission.decide(product(weight=4.5), DELIVERY, STATION, state())
    assert alone[0] == admission.ACCEPTED

    after_hot_job = admission.decide(
        product(weight=4.5), DELIVERY, STATION, state(),
        committed={"orders": 1, "energy_wh": 0.0, "peak_temperature_c": 68.0})
    assert after_hot_job[0] == admission.REFUSED
    assert "cool down" in after_hot_job[1]


def test_the_classifier_is_asked_about_the_projected_robot():
    """The model must see the same world the gates judged, or the two disagree silently."""
    seen = {}

    def spy(features):
        seen.update(features)
        return True, 0.9, "model"

    admission.decide(product(), DELIVERY, STATION, state(battery=90.0), predictor=spy,
                     committed={"orders": 1, "energy_wh": 4.4, "peak_temperature_c": None})
    assert seen["battery_percent"] < 90.0


def test_the_cost_reports_what_was_reserved():
    _, _, _, cost = admission.decide(
        product(), DELIVERY, STATION, state(battery=90.0),
        committed={"orders": 2, "energy_wh": 4.4, "peak_temperature_c": None})
    assert cost["reserved_orders"] == 2
    assert cost["reserved_energy_wh"] == pytest.approx(4.4)
    assert cost["battery_at_start_percent"] < 90.0


def test_a_flat_battery_is_not_mistaken_for_a_missing_reading():
    """0.0 is a reading, not an absence: `state.get(x) or default` calls a flat robot healthy."""
    decision, reason, _, cost = admission.decide(product(), DELIVERY, STATION,
                                                 state(battery=0.0))
    assert decision == admission.REFUSED
    assert "reserve" in reason
    assert cost["battery_at_start_percent"] == 0.0


def test_a_broken_robot_is_not_mistaken_for_a_missing_reading():
    decision, reason, _, _ = admission.decide(product(), DELIVERY, STATION,
                                              state(condition=0.0))
    assert decision == admission.REFUSED
    assert "maintenance" in reason


def test_features_are_the_five_the_model_was_trained_on():
    cost = admission.estimate(product(), DELIVERY, STATION, state())
    features = admission.features(product(), cost, state())
    assert set(features) == {"battery_percent", "temperature_c", "condition_percent",
                             "payload_kg", "route_distance_m"}
    assert all(isinstance(v, float) for v in features.values())


def test_missing_telemetry_falls_back_to_a_healthy_robot():
    """Before the first telemetry sample the state row is empty; that must not crash."""
    decision, _, _, cost = admission.decide(product(), DELIVERY, STATION, {})
    assert decision == admission.ACCEPTED
    assert cost["energy_wh"] > 0
