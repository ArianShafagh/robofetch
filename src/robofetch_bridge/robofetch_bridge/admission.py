"""Order admission and cost preview (proposal §3.3 C2, §11.2).

The decision procedure that turns "a customer wants SKU-3001 at delivery_2" into either

    accepted · 4.2 m + 5.6 m, 6.1 Wh, 62 s
    refused  · battery would drop to 11%, below the 15% reserve needed to reach the station

This is the module the proposal defends as a *complex* functionality rather than a
third-party ML call, so it is worth being precise about why:

  * the classifier never sees an order. It sees a feature vector assembled here from
    telemetry, the product catalogue and a route this module computes;
  * its answer is one input among several. Every policy gate below can overrule it - a
    classifier that says "feasible" is still refused if the robot could not physically get
    back to its station afterwards;
  * there is a RESERVATION LEDGER. Every decision is taken against the robot as it will be
    once the orders already in flight are finished, not as telemetry currently reports it.
    Judging each order against the same live battery is how an admission controller ends up
    promising more than the machine has - see `project`;
  * when the AI service cannot be reached the workflow still produces a decision, using the
    deterministic model alone (NFR2). The system never blocks on the AI.

Pure logic: no ROS, no HTTP, no database handles. Everything it needs is passed in, which is
what makes the whole decision path unit-testable without a simulator.
"""
from robofetch_core.robot_model import (CONDITION_MIN, EFFECTIVE_SPEED, RESERVE_PERCENT,
                                        RobotCondition, T_MAX, battery_percent_for, duration_s,
                                        energy_wh, route_legs, simulate_route)

ACCEPTED = "accepted"
REFUSED = "refused"


def reading(robot_state, field, default):
    """One telemetry field, with a sane value when the robot has not reported yet.

    Written out rather than using `state.get(field) or default`, which cannot tell a missing
    reading from a genuine zero - and every zero here means something serious: 0% battery is a
    flat robot, 0% condition is a broken one. That idiom would silently report both as healthy,
    which is the one direction an admission check must never be wrong in.
    """
    value = robot_state.get(field)
    return default if value is None else float(value)


def _condition_from_state(robot_state):
    """Build the model's condition object from whatever the telemetry last reported."""
    return RobotCondition(
        battery_percent=reading(robot_state, "battery_percent", 100.0),
        temperature_c=reading(robot_state, "temperature_c", 22.0),
        condition_percent=reading(robot_state, "condition_percent", 100.0),
        cumulative_distance_m=reading(robot_state, "cumulative_distance_m", 0.0),
    )


def project(robot_state, committed=None):
    """Roll the robot's state forward past work it has already been promised to.

    Telemetry reports what the robot IS. Admission has to reason about what it WILL BE by the
    time this new order starts, because every order already accepted and not yet finished is
    going to spend energy and make heat first. Judging against the live reading instead lets
    two orders be admitted against the same battery, and the robot strands itself honouring
    both - a mistake that only shows up under load, which is exactly when it matters.

    `committed` is Database.committed_load(): summed energy and peak temperature over the
    unfinished orders. Deliberately conservative in two small ways - each order reserves a
    return leg although the robot only drives home once, and the classifier is then asked about
    a robot flatter and hotter than the one it can see. Over-reserving refuses a job that might
    just have fitted; under-reserving strands the robot. Only one of those is recoverable.
    """
    projected = dict(robot_state)
    if not committed:
        return projected

    # NOT clamped at zero. A negative projected battery is not a physical claim, it is the
    # size of the hole: the queue already promises more than the pack holds. Clamping would
    # throw that away and make a hopeless order look merely marginal.
    projected["battery_percent"] = (
        reading(robot_state, "battery_percent", 100.0)
        - battery_percent_for(float(committed.get("energy_wh") or 0.0)))

    peak = committed.get("peak_temperature_c")
    if peak is not None:
        # Heat does not add up the way energy does: the motors reach the temperature of the
        # hottest job in the queue, and cool between jobs rather than accumulating.
        projected["temperature_c"] = max(
            reading(robot_state, "temperature_c", 22.0), float(peak))
    return projected


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


def estimate_return(station, robot_state, robot_xy=None, committed=None):
    """Cost the drive home, so the operator sees what they are asking for before confirming.

    There are no gates on this and there deliberately never will be. Admission control exists
    to stop the robot accepting work it cannot finish; going to the station is the one task that
    leaves a struggling robot in a BETTER state than it started. Refusing a flat, hot robot
    permission to go and charge would be precisely backwards.

    It is still costed, and still reserved, because the drive really does spend battery - the
    next delivery must be judged against the robot that arrives home, not the one standing at
    the bay.
    """
    state = project(robot_state, committed)
    condition = _condition_from_state(state)
    start = robot_xy or (station["x"], station["y"])
    distance = ((start[0] - station["x"]) ** 2 + (start[1] - station["y"]) ** 2) ** 0.5

    energy = energy_wh(distance, 0.0, condition.temperature_c, condition.condition_percent)
    predicted, peak = simulate_route(condition, distance, 0.0)

    return {
        "distance_m": round(distance, 2),
        "return_distance_m": 0.0,          # this IS the return leg; nothing follows it
        "total_distance_m": round(distance, 2),
        "energy_wh": round(energy, 2),
        "return_energy_wh": 0.0,
        # No FIXED_OVERHEAD_S: there is no parcel to pick up or put down.
        "seconds": round(distance / EFFECTIVE_SPEED, 1),
        "payload_kg": 0.0,
        "battery_after_percent": round(
            condition.battery_percent - battery_percent_for(energy), 1),
        "peak_temperature_c": round(peak, 1),
        "condition_after_percent": round(predicted.condition_percent, 1),
        "reserved_energy_wh": round(float((committed or {}).get("energy_wh") or 0.0), 2),
        "reserved_orders": int((committed or {}).get("orders") or 0),
        "battery_at_start_percent": round(reading(state, "battery_percent", 100.0), 1),
    }


def features(product, cost, robot_state):
    """The feature vector handed to the classifier.

    Deliberately small and physically meaningful, so the model's behaviour can be reasoned
    about and the report can explain what it keys on.
    """
    return {
        "battery_percent": reading(robot_state, "battery_percent", 100.0),
        "temperature_c": reading(robot_state, "temperature_c", 22.0),
        "condition_percent": reading(robot_state, "condition_percent", 100.0),
        "payload_kg": float(product["weight_kg"]),
        "route_distance_m": float(cost["total_distance_m"]),
    }


def decide(product, delivery, station, robot_state, robot_xy=None, predictor=None,
           committed=None):
    """Run the full admission workflow. Returns (decision, reason, decided_by, cost).

    The gate ORDER is deliberate: the cheap, certain, physical checks run first, so a refusal
    the operator can act on ("out of stock", "needs charging") is never masked by a model
    verdict. The classifier is consulted last and can only ever refuse an order the physical
    checks already allowed - it is never able to approve one they rejected.

    Everything below judges the PROJECTED robot - the one that will exist once the orders
    already in flight are done - not the one telemetry is describing right now. See `project`.
    """
    state = project(robot_state, committed)
    cost = estimate(product, delivery, station, state, robot_xy)
    decided_by = "policy"

    reserved_energy = float((committed or {}).get("energy_wh") or 0.0)
    reserved_orders = int((committed or {}).get("orders") or 0)
    cost["reserved_energy_wh"] = round(reserved_energy, 2)
    cost["reserved_orders"] = reserved_orders
    cost["battery_at_start_percent"] = round(reading(state, "battery_percent", 100.0), 1)

    # Refusals caused by the reservation need to say so, or the operator reads "battery would
    # fall to 8%" next to a robot page showing 74% and concludes the system is broken.
    queued = (f" once the {reserved_orders} order(s) already in progress are done"
              if reserved_orders else "")

    # --- gate 1: is there anything to fetch?
    if int(product.get("stock", 0)) <= 0:
        return REFUSED, f"{product['product_id']} is out of stock", decided_by, cost

    # --- gate 2: could the robot still reach its station afterwards?
    if cost["battery_after_percent"] < RESERVE_PERCENT:
        return (REFUSED,
                f"battery would fall to {cost['battery_after_percent']:.0f}%{queued}, below "
                f"the {RESERVE_PERCENT:.0f}% reserve needed to return to the station",
                decided_by, cost)

    # --- gate 3: would it overheat part-way?
    if cost["peak_temperature_c"] >= T_MAX:
        return (REFUSED,
                f"motors would reach {cost['peak_temperature_c']:.0f} C{queued}, at or above "
                f"the {T_MAX:.0f} C limit - the robot needs to cool down first",
                decided_by, cost)

    # --- gate 4: is the robot healthy enough to be working at all?
    # Wear is the one quantity NOT projected forward: it degrades over the whole life of the
    # robot, not measurably within a queue, so the live reading is the honest one.
    condition_now = reading(robot_state, "condition_percent", 100.0)
    if condition_now < CONDITION_MIN:
        return (REFUSED,
                f"robot condition is {condition_now:.0f}%, below the {CONDITION_MIN:.0f}% "
                "minimum - maintenance required",
                decided_by, cost)

    # --- gate 5: the learned verdict, consulted only once the physics allows the job
    if predictor is not None:
        verdict = predictor(features(product, cost, state))
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
