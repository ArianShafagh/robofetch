"""Order admission and cost preview (proposal §3.3 C2, §11.2).

The decision procedure that turns "a customer wants SKU-3001 at delivery_2" into either

    accepted · 4.2 m + 5.6 m, 6.1 Wh, 62 s
    refused  · battery would drop to 11%, below the 15% reserve needed to reach the station

This is the module the proposal defends as a *complex* functionality rather than a
third-party ML call, so it is worth being precise about why:

  * the classifier never sees an order. It sees a feature vector assembled here from live
    telemetry, the product catalogue and a route this module computes;
  * its answer is one input among several. Every policy gate below can overrule it - a
    classifier that says "feasible" is still refused if the robot could not physically get
    back to its station afterwards;
  * when the AI service cannot be reached the workflow still produces a decision, using the
    deterministic model alone (NFR2). The system never blocks on the AI.

Pure logic: no ROS, no HTTP, no database handles. Everything it needs is passed in, which is
what makes the whole decision path unit-testable without a simulator.
"""
from robofetch_core.robot_model import (CONDITION_MIN, RESERVE_PERCENT, RobotCondition,
                                        T_MAX, battery_percent_for, duration_s, energy_wh,
                                        route_legs, simulate_route)

ACCEPTED = "accepted"
REFUSED = "refused"


def _condition_from_state(robot_state):
    """Build the model's condition object from whatever the telemetry last reported."""
    return RobotCondition(
        battery_percent=float(robot_state.get("battery_percent") or 100.0),
        temperature_c=float(robot_state.get("temperature_c") or 22.0),
        condition_percent=float(robot_state.get("condition_percent") or 100.0),
        cumulative_distance_m=float(robot_state.get("cumulative_distance_m") or 0.0),
    )


def estimate(product, delivery, station, robot_state, robot_xy=None):
    """Cost the job. Returns the route, the energy split and the predicted end state.

    The return leg is costed even though the customer never sees it, because a job the robot
    cannot come back from is not a job it can take.
    """
    condition = _condition_from_state(robot_state)
    payload = float(product["weight_kg"])
    start = robot_xy or (station["x"], station["y"])

    legs = route_legs(start,
                      (product["pick_x"], product["pick_y"]),
                      (delivery["x"], delivery["y"]),
                      (station["x"], station["y"]))

    order_energy = energy_wh(legs["order_m"], payload,
                             condition.temperature_c, condition.condition_percent)
    # The return leg is driven empty, so it costs less per metre than the loaded legs.
    return_energy = energy_wh(legs["return_m"], 0.0,
                              condition.temperature_c, condition.condition_percent)

    predicted, peak_temperature = simulate_route(condition, legs["order_m"], payload)

    return {
        "distance_m": round(legs["order_m"], 2),
        "return_distance_m": round(legs["return_m"], 2),
        "total_distance_m": round(legs["total_m"], 2),
        "energy_wh": round(order_energy, 2),
        "return_energy_wh": round(return_energy, 2),
        "seconds": round(duration_s(legs["order_m"]), 1),
        "payload_kg": payload,
        "battery_after_percent": round(
            condition.battery_percent - battery_percent_for(order_energy + return_energy), 1),
        "peak_temperature_c": round(peak_temperature, 1),
        "condition_after_percent": round(predicted.condition_percent, 1),
    }


def features(product, cost, robot_state):
    """The feature vector handed to the classifier.

    Deliberately small and physically meaningful, so the model's behaviour can be reasoned
    about and the report can explain what it keys on.
    """
    return {
        "battery_percent": float(robot_state.get("battery_percent") or 100.0),
        "temperature_c": float(robot_state.get("temperature_c") or 22.0),
        "condition_percent": float(robot_state.get("condition_percent") or 100.0),
        "payload_kg": float(product["weight_kg"]),
        "route_distance_m": float(cost["total_distance_m"]),
    }


def decide(product, delivery, station, robot_state, robot_xy=None, predictor=None):
    """Run the full admission workflow. Returns (decision, reason, decided_by, cost).

    The gate ORDER is deliberate: the cheap, certain, physical checks run first, so a refusal
    the operator can act on ("out of stock", "needs charging") is never masked by a model
    verdict. The classifier is consulted last and can only ever refuse an order the physical
    checks already allowed - it is never able to approve one they rejected.
    """
    cost = estimate(product, delivery, station, robot_state, robot_xy)
    decided_by = "policy"

    # --- gate 1: is there anything to fetch?
    if int(product.get("stock", 0)) <= 0:
        return REFUSED, f"{product['product_id']} is out of stock", decided_by, cost

    # --- gate 2: could the robot still reach its station afterwards?
    if cost["battery_after_percent"] < RESERVE_PERCENT:
        return (REFUSED,
                f"battery would fall to {cost['battery_after_percent']:.0f}%, below the "
                f"{RESERVE_PERCENT:.0f}% reserve needed to return to the station",
                decided_by, cost)

    # --- gate 3: would it overheat part-way?
    if cost["peak_temperature_c"] >= T_MAX:
        return (REFUSED,
                f"motors would reach {cost['peak_temperature_c']:.0f} C, at or above the "
                f"{T_MAX:.0f} C limit - the robot needs to cool down first",
                decided_by, cost)

    # --- gate 4: is the robot healthy enough to be working at all?
    condition_now = float(robot_state.get("condition_percent") or 100.0)
    if condition_now < CONDITION_MIN:
        return (REFUSED,
                f"robot condition is {condition_now:.0f}%, below the {CONDITION_MIN:.0f}% "
                "minimum - maintenance required",
                decided_by, cost)

    # --- gate 5: the learned verdict, consulted only once the physics allows the job
    if predictor is not None:
        verdict = predictor(features(product, cost, robot_state))
        if verdict is not None:
            feasible, confidence, source = verdict
            decided_by = source
            if not feasible:
                return (REFUSED,
                        f"predicted infeasible in the robot's current condition "
                        f"(confidence {confidence:.0%})",
                        decided_by, cost)

    return (ACCEPTED,
            f"{cost['distance_m']:.1f} m, {cost['energy_wh']:.1f} Wh, "
            f"~{cost['seconds']:.0f} s; battery {cost['battery_after_percent']:.0f}% after "
            "returning to the station",
            decided_by, cost)
