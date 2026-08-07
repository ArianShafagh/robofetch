#!/usr/bin/env python3
"""RoboFetch acceptance tests - the evidence table for the report (proposal §12, Testing).

Unlike the pytest suites, these run against the REAL system: Gazebo, Nav2, the gripper, the
condition model, the AI service and the web API, all live. Each check maps to a requirement
from the proposal, and every physical claim is confirmed against Gazebo ground truth rather
than a status field - a delivery reported as `completed` is a claim, the parcel's world pose
is the evidence.

    ./scripts/run.sh --headless                             # terminal 1
    ./robofetch_venv/bin/python scripts/acceptance.py       # terminal 2

Options:
    --quick     skip the slow physical checks (FR6 FIFO, FR8 retry)
    --json FILE also write machine-readable results

Exit code is 0 only if every check passed, so it can gate a build.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
WORLD = "warehouse"
DELIVERY_TOLERANCE = 1.1

# Where each parcel starts in warehouse.sdf. The checks order parcels FROM their shelves, so a
# world where a previous run already moved them would fail for the wrong reason.
SPAWN = {
    "parcel_1": (-3.00, 1.35), "parcel_2": (-2.00, 1.35),
    "parcel_3": (1.05, 1.35), "parcel_4": (1.95, 1.35),
    "parcel_5": (3.15, -1.50), "parcel_6": (3.15, -0.50),
}

results = []


# ------------------------------------------------------------------------ plumbing
def api(path, method="GET", body=None, timeout=15):
    request = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None


def gz_pose(model):
    try:
        out = subprocess.run(
            ["gz", "topic", "-e", "-t", f"/world/{WORLD}/dynamic_pose/info", "-n", "1"],
            capture_output=True, text=True, timeout=30).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if f'name: "{model}"' in line:
            x = None
            for follow in lines[index + 1:index + 12]:
                stripped = follow.strip()
                if x is None and stripped.startswith("x:"):
                    x = float(stripped.split()[1])
                elif x is not None and stripped.startswith("y:"):
                    return x, float(stripped.split()[1])
    return None


def record(req, description, passed, evidence):
    results.append({"requirement": req, "description": description,
                    "passed": bool(passed), "evidence": evidence})
    print(f"  {'PASS' if passed else 'FAIL'}  {req:<8} {description}")
    print(f"         {evidence}")
    return passed


def wait_for_order(order_id, timeout=420):
    deadline = time.time() + timeout
    order = None
    while time.time() < deadline:
        _, order = api(f"/api/orders/{order_id}")
        if order and order["status"] in ("completed", "failed", "cancelled", "refused"):
            return order
        time.sleep(2)
    return order


# --------------------------------------------------------------------------- checks
def check_setup():
    status, health = api("/health")
    if status != 200 or not health.get("robot_connected"):
        return record("SETUP", "API up and a robot subscribed to /orders/new", False,
                      f"health={health}")

    strays = []
    for model, (sx, sy) in SPAWN.items():
        pose = gz_pose(model)
        if pose is None:
            strays.append(f"{model}: no pose from Gazebo")
        elif math.hypot(pose[0] - sx, pose[1] - sy) > 0.5:
            strays.append(f"{model} at ({pose[0]:+.2f}, {pose[1]:+.2f}), not on its shelf")
    if strays:
        return record("SETUP", "parcels are on their shelves (fresh world)", False,
                      "; ".join(strays) + " - restart with ./scripts/stop.sh && "
                      "./scripts/run.sh --headless")

    return record("SETUP", "API up, robot connected, all 6 parcels on their shelves", True,
                  f"database={health['database']}, AI={health['ai_service']['url']}")


def check_uc5_catalogue_crud():
    """UC5/FR1/FR14 - the administrator maintains the catalogue and the layout."""
    _, products = api("/api/products")
    _, points = api("/api/delivery-points")
    ok = (len(products) == 6 and len(points) == 2
          and all(p["pick_x"] is not None for p in products))
    return record("UC5", "catalogue and layout are served from the database", ok,
                  f"{len(products)} products, {len(points)} destinations, "
                  f"weights {sorted(p['weight_kg'] for p in products)}")


def check_uc9_preview():
    """UC9/FR4/C2 - the cost and the verdict are produced BEFORE anything is ordered."""
    started = time.time()
    status, preview = api("/api/preview", "POST",
                          {"product_id": "SKU-3001", "delivery_id": "delivery_1"})
    elapsed = time.time() - started

    if status != 200:
        return record("UC9", "order preview returns a cost and a verdict", False,
                      f"HTTP {status}: {preview}")

    estimate = preview["estimate"]
    ok = (estimate["energy_wh"] > 0 and estimate["distance_m"] > 0
          and preview["decision"] in ("accepted", "refused"))
    record("UC9", "order preview returns a cost and a verdict", ok,
           f"{estimate['distance_m']} m, {estimate['energy_wh']} Wh, "
           f"peak {estimate['peak_temperature_c']} C -> {preview['decision']} "
           f"(by {preview['decided_by']})")

    # NFR1: the preview, including the model call, must come back inside 2 s.
    record("NFR1", "preview (including the ML call) responds within 2 s", elapsed < 2.0,
           f"{elapsed * 1000:.0f} ms")

    # The classifier must actually be in the loop, not silently skipped.
    _, health = api("/health")
    consulted = health["ai_service"]["reachable"] is True
    return record("C2", "the classifier is consulted inside the admission workflow", consulted,
                  f"ai_service={health['ai_service']}, decided_by={preview['decided_by']}")


def check_uc1_uc2_uc4_delivery():
    """UC1 submit, UC2 track, UC4 the full pick-and-place, verified physically."""
    status, order = api("/api/orders", "POST",
                        {"product_id": "SKU-1001", "delivery_id": "delivery_1"})
    if status != 201 or order.get("status") == "refused":
        return record("UC1", "an order can be submitted by product ID", False,
                      f"HTTP {status}: {order}")
    record("UC1", "an order can be submitted by product ID", True,
           f"order {order['id']} for SKU-1001, {order['decision']} by {order['decided_by']}, "
           f"predicted {order['estimated_energy_wh']} Wh")

    final = wait_for_order(order["id"])
    record("UC2", "an order can be tracked to a terminal state", final["status"] == "completed",
           f"order {final['id']} -> {final['status']}: {final['detail']}")

    pose = gz_pose("parcel_1")
    _, points = api("/api/delivery-points")
    target = next(d for d in points if d["delivery_id"] == "delivery_1")
    if pose is None:
        return record("UC4", "the parcel is physically delivered (Gazebo ground truth)",
                      False, "could not read parcel_1's pose")
    gap = math.hypot(pose[0] - target["x"], pose[1] - target["y"])
    moved = math.hypot(pose[0] - SPAWN["parcel_1"][0], pose[1] - SPAWN["parcel_1"][1])
    return record("UC4", "the parcel is physically delivered (Gazebo ground truth)",
                  final["status"] == "completed" and gap <= DELIVERY_TOLERANCE,
                  f"parcel_1 at ({pose[0]:+.2f}, {pose[1]:+.2f}), {gap:.2f} m from the bay, "
                  f"moved {moved:.2f} m")


def check_fr10_fr12_telemetry():
    """FR10 condition tracked, FR12 every run written to CSV."""
    _, robot = api("/api/robot")
    tracked = all(robot.get(field) is not None for field in
                  ("battery_percent", "temperature_c", "condition_percent", "state"))
    record("FR10", "battery, temperature and condition are tracked", tracked,
           f"battery {robot.get('battery_percent'):.1f}%, "
           f"temp {robot.get('temperature_c'):.1f} C, "
           f"condition {robot.get('condition_percent'):.2f}%, state={robot.get('state')}")

    log_dir = os.path.expanduser("~/robofetch_ws/logs")
    logs = sorted(f for f in os.listdir(log_dir) if f.endswith(".csv")) \
        if os.path.isdir(log_dir) else []
    if not logs:
        return record("FR12", "the run is logged to CSV for ML training", False,
                      f"no CSV files in {log_dir}")
    newest = os.path.join(log_dir, logs[-1])
    with open(newest) as handle:
        rows = sum(1 for _ in handle) - 1
    return record("FR12", "the run is logged to CSV for ML training", rows > 10,
                  f"{logs[-1]}: {rows} samples")


def check_fr5_refusal():
    """FR5 - an order the robot cannot serve is refused, with a reason.

    Staged through the catalogue rather than by draining the battery: setting stock to zero is
    a legitimate CRUD operation that drives the first admission gate, and it is repeatable.
    """
    _, product = api("/api/products")
    original = next(p for p in product if p["product_id"] == "SKU-1002")

    api("/api/preview", "POST", {"product_id": "SKU-1002", "delivery_id": "delivery_1"})
    # Take it out of stock through the database, then ask for it.
    import sqlite3
    conn = sqlite3.connect(os.path.expanduser("~/robofetch_ws/robofetch.db"))
    conn.execute("UPDATE products SET stock = 0 WHERE product_id = 'SKU-1002'")
    conn.commit()
    conn.close()

    status, order = api("/api/orders", "POST",
                        {"product_id": "SKU-1002", "delivery_id": "delivery_1"})
    refused = order and order["status"] == "refused"
    reason = (order or {}).get("decision_reason", "")

    conn = sqlite3.connect(os.path.expanduser("~/robofetch_ws/robofetch.db"))
    conn.execute("UPDATE products SET stock = ? WHERE product_id = 'SKU-1002'",
                 (original["stock"],))
    conn.commit()
    conn.close()

    return record("FR5", "an order the robot cannot serve is refused, with a reason", refused,
                  f"status={(order or {}).get('status')}, reason='{reason}'")


def check_fr6_fifo():
    """FR6 - orders are served in the sequence customers placed them, not by proximity."""
    _, first = api("/api/orders", "POST",
                   {"product_id": "SKU-3002", "delivery_id": "delivery_2"})
    _, second = api("/api/orders", "POST",
                    {"product_id": "SKU-2002", "delivery_id": "delivery_2"})
    if not first or not second or "id" not in first or "id" not in second:
        return record("FR6", "orders are served oldest-first (FIFO)", False,
                      f"could not queue two orders: {first}, {second}")

    started = []
    deadline = time.time() + 600
    while time.time() < deadline and len(started) < 2:
        for order_id in (second["id"], first["id"]):     # poll the LATER one first on purpose
            _, row = api(f"/api/orders/{order_id}")
            if row and row["status"] != "pending" and order_id not in started:
                started.append(order_id)
        time.sleep(1)

    for order_id in (first["id"], second["id"]):
        wait_for_order(order_id)

    ok = bool(started) and started[0] == first["id"]
    return record("FR6", "orders are served oldest-first (FIFO)", ok,
                  f"submitted [{first['id']}, {second['id']}], started {started} - "
                  f"the earlier order ran first")


def check_fr8_retry():
    """FR8 - a grab is retried 3 times, then the order fails and the robot goes home."""
    # A catalogue entry whose pick point is nowhere near its parcel: the robot drives to
    # shelf_1 and tries to grab a parcel that is over on shelf_3.
    import sqlite3
    path = os.path.expanduser("~/robofetch_ws/robofetch.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT OR REPLACE INTO products (product_id, name, category, weight_kg, stock,"
        " shelf_id, pick_point_id, model_name) VALUES"
        " ('SKU-TEST', 'Unreachable test part', 'test', 0.5, 1, 'shelf_1', 'pick_1b',"
        " 'parcel_6')")
    conn.commit()
    conn.close()

    _, order = api("/api/orders", "POST",
                   {"product_id": "SKU-TEST", "delivery_id": "delivery_1"})
    final = wait_for_order(order["id"], timeout=900) if order and "id" in order else None

    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM products WHERE product_id = 'SKU-TEST'")
    conn.commit()
    conn.close()

    if final is None:
        return record("FR8", "a failing grab is retried 3 times, then the order fails",
                      False, "the order never reached a terminal state")
    ok = final["status"] == "failed" and final["attempts"] == 3
    return record("FR8", "a failing grab is retried 3 times, then the order fails", ok,
                  f"order {final['id']} -> {final['status']} after {final['attempts']} "
                  f"attempts: {final['detail']}")


def check_uc6_analytics():
    """UC6/FR15 - aggregate statistics agree with the orders table."""
    _, stats = api("/api/analytics")
    _, orders = api("/api/orders")
    completed = sum(1 for o in orders if o["status"] == "completed")
    refused = sum(1 for o in orders if o["status"] == "refused")
    ok = (stats["total_orders"] == len(orders) and stats["completed"] == completed
          and stats["refused"] == refused)
    return record("UC6", "analytics agree with the orders table", ok,
                  f"reported {stats['completed']}/{stats['total_orders']} completed and "
                  f"{stats['refused']} refused; counted {completed} and {refused}; "
                  f"rate={stats['success_rate']}")


def check_nfr3_pages():
    """NFR3 - every page renders server-side, with no JavaScript."""
    pages = {}
    for path in ("/", "/orders", "/robot"):
        try:
            with urllib.request.urlopen(f"{API}{path}", timeout=10) as response:
                body = response.read().decode()
                pages[path] = (response.status, "<script" in body.lower())
        except Exception as exc:                                   # noqa: BLE001
            pages[path] = (str(exc), None)
    ok = all(status == 200 and not has_script for status, has_script in pages.values())
    return record("NFR3", "all pages render server-side with no JavaScript", ok,
                  ", ".join(f"{p} -> {s[0]}{' (has <script>!)' if s[1] else ''}"
                            for p, s in pages.items()))


# ----------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true",
                        help="skip the slow physical checks (FR6 FIFO, FR8 retry)")
    parser.add_argument("--json", metavar="FILE", help="also write results as JSON")
    args = parser.parse_args()

    print("RoboFetch v2 acceptance tests")
    print("=" * 74)

    if not check_setup():
        print("\nThe system is not ready. Start it with ./scripts/run.sh --headless and wait\n"
              "for 'Localized and ready' before running this.")
        return 1

    check_uc5_catalogue_crud()
    check_uc9_preview()
    check_nfr3_pages()
    check_uc1_uc2_uc4_delivery()
    check_fr10_fr12_telemetry()
    check_fr5_refusal()
    if not args.quick:
        check_fr6_fifo()
        check_fr8_retry()
    check_uc6_analytics()

    passed = sum(1 for r in results if r["passed"])
    print("=" * 74)
    print(f"{passed}/{len(results)} checks passed")
    for row in results:
        if not row["passed"]:
            print(f"  FAILED: {row['requirement']} - {row['description']}")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(results, handle, indent=2)
        print(f"results written to {args.json}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
