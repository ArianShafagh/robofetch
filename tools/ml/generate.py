#!/usr/bin/env python3
"""Synthetic run generator - DISPOSABLE (proposal §10.5, NFR6).

Real runs produce CSV logs, but a delivery takes about a minute, so collecting enough labelled
examples to train on would take days. This script fabricates them instead, by simulating runs
through **the same equations the live robot uses** - it imports `robofetch_core.robot_model`
rather than reimplementing the physics, so a model trained here is at least consistent with the
system it will be deployed into.

    source install/setup.bash
    ./robofetch_venv/bin/python tools/ml/generate.py --runs 4000 --out tools/ml/runs.csv

Output schema is IDENTICAL to the live per-run logger, so the two files are interchangeable
and can be concatenated.

THIS FILE IS MEANT TO BE DELETED once the model is trained. Nothing in the running system
imports it; the deployed service depends only on the exported model.joblib. Be honest about
this in the report: the classifier learns THIS GENERATOR'S assumptions, not real robot
physics. Its purpose is to demonstrate an ML-in-the-loop decision workflow.
"""
import argparse
import csv
import random

from robofetch_core.robot_model import (CONDITION_MIN, RESERVE_PERCENT, RobotCondition, T_MAX,
                                        battery_percent_for, duration_s, energy_wh,
                                        simulate_route)

# Route lengths that actually occur in this warehouse. The shortest job is a pick point near
# the station; the longest is the far shelf to the opposite corner, about 12 m. Sampling
# outside that range would teach the model about journeys this robot can never be asked to
# make.
MIN_DISTANCE_M, MAX_DISTANCE_M = 2.0, 12.0
# The station is central, so the return leg barely varies whichever corner it comes from.
MIN_RETURN_M, MAX_RETURN_M = 2.5, 3.5
PAYLOADS_KG = [0.3, 0.6, 1.1, 1.8, 3.2, 4.5]      # the six catalogue weights

FIELDS = ["run_id", "battery_percent", "temperature_c", "condition_percent", "payload_kg",
          "route_distance_m", "energy_wh", "duration_s", "peak_temperature_c",
          "battery_after_percent", "condition_after_percent", "completed"]


def simulate_one(run_id, rng):
    """One hypothetical order, labelled with whether the robot could actually have done it."""
    # Sample a plausible starting condition. The ranges are wide on purpose: the classifier
    # has to see exhausted, overheated and worn robots to learn where the boundary is.
    start = RobotCondition(
        battery_percent=rng.uniform(5.0, 100.0),
        temperature_c=rng.uniform(22.0, 68.0),
        condition_percent=rng.uniform(25.0, 100.0),
    )
    payload = rng.choice(PAYLOADS_KG)
    distance = rng.uniform(MIN_DISTANCE_M, MAX_DISTANCE_M)
    return_distance = rng.uniform(MIN_RETURN_M, MAX_RETURN_M)

    order_energy = energy_wh(distance, payload, start.temperature_c, start.condition_percent)
    return_energy = energy_wh(return_distance, 0.0, start.temperature_c,
                              start.condition_percent)
    predicted, peak = simulate_route(start, distance, payload)

    battery_after = start.battery_percent - battery_percent_for(order_energy + return_energy)

    # --- the label: could this order really have been completed?
    # Measurement noise on the thresholds keeps the boundary from being a razor edge, which
    # is what stops the classifier collapsing into a trivial linear rule.
    completed = (
        battery_after >= RESERVE_PERCENT * rng.uniform(0.9, 1.1)
        and peak < T_MAX * rng.uniform(0.97, 1.03)
        and start.condition_percent >= CONDITION_MIN * rng.uniform(0.9, 1.1)
    )

    return {
        "run_id": run_id,
        "battery_percent": round(start.battery_percent, 2),
        "temperature_c": round(start.temperature_c, 2),
        "condition_percent": round(start.condition_percent, 2),
        "payload_kg": payload,
        "route_distance_m": round(distance + return_distance, 2),
        "energy_wh": round(order_energy, 3),
        "duration_s": round(duration_s(distance), 1),
        "peak_temperature_c": round(peak, 2),
        "battery_after_percent": round(battery_after, 2),
        "condition_after_percent": round(predicted.condition_percent, 2),
        "completed": int(completed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=4000)
    parser.add_argument("--out", default="tools/ml/runs.csv")
    parser.add_argument("--seed", type=int, default=42, help="fixed so runs are reproducible")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    rows = [simulate_one(f"synthetic-{i:05d}", rng) for i in range(args.runs)]

    with open(args.out, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    feasible = sum(r["completed"] for r in rows)
    print(f"wrote {len(rows)} runs to {args.out}")
    print(f"  feasible: {feasible} ({feasible / len(rows):.1%})")
    print(f"  infeasible: {len(rows) - feasible}")
    if not 0.2 < feasible / len(rows) < 0.8:
        print("  WARNING: classes are badly unbalanced; adjust the sampling ranges.")


if __name__ == "__main__":
    main()
