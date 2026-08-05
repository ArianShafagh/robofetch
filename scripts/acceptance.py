#!/usr/bin/env python3
"""RoboFetch acceptance tests - the evidence table for the report (M9).

Unlike the pytest suites, these run against the REAL system: Gazebo, Nav2, the gripper and
the web API, all live. Each check maps to a use case or requirement from the proposal, and
every physical claim is confirmed against Gazebo ground truth rather than against a status
field - a delivery reported as "completed" is a claim, the parcel's world pose is the
evidence (HANDOVER 5.4).

    ./scripts/run.sh --headless          # terminal 1: bring the system up
    ./robofetch_venv/bin/python scripts/acceptance.py     # terminal 2

Options:
    --quick     skip the two slow checks (FR4 retry, scheduler ordering)
    --json FILE also write machine-readable results

Exit code is 0 only if every check passed, so it can gate a build.
"""
import argparse
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
WORLD = "warehouse"

# How close a parcel must end up to count as delivered. Matches the task manager's
# delivery_tolerance; the dominant error is DetachableJoint welding the parcel wherever it
# lies rather than at a fixed carry point (HANDOVER 7).
DELIVERY_TOLERANCE = 1.1

results = []


# ------------------------------------------------------------------------ plumbing
def api(path, method="GET", body=None, timeout=10):
    request = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def gz_pose(model):
    """The model's REAL position in Gazebo, or None."""
    try:
        out = subprocess.run(
            ["gz", "topic", "-e", "-t", f"/world/{WORLD}/dynamic_pose/info", "-n", "1"],
            capture_output=True, text=True, timeout=30).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if f'name: "{model}"' in line:
            # position{} comes before orientation{}, so the first x:/y: after the name are
            # the ones we want.
            x = y = None
            for follow in lines[index + 1:index + 12]:
                stripped = follow.strip()
                if x is None and stripped.startswith("x:"):
                    x = float(stripped.split()[1])
                elif x is not None and y is None and stripped.startswith("y:"):
                    y = float(stripped.split()[1])
                    return x, y
    return None


def record(req, description, passed, evidence):
    results.append({"requirement": req, "description": description,
                    "passed": bool(passed), "evidence": evidence})
    print(f"  {'PASS' if passed else 'FAIL'}  {req:<7} {description}")
    print(f"        {evidence}")
    return passed


def wait_for_order(order_id, timeout=420):
    """Block until the order reaches a terminal state; returns the final row."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, order = api(f"/orders/{order_id}")
        if order and order["status"] in ("completed", "failed", "cancelled"):
            return order
        time.sleep(2)
    _, order = api(f"/orders/{order_id}")
    return order


# --------------------------------------------------------------------------- checks
# Where each parcel starts in warehouse.sdf. The checks below order parcels FROM their
# shelves, so a world where a previous run already moved them would fail for the wrong
# reason - the robot would drive to an empty shelf and correctly refuse to grab nothing.
SPAWN = {"item_1": (-2.5, 1.35), "item_2": (1.5, 1.35), "item_3": (3.15, -1.0)}


def check_system_ready():
    status, health = api("/health")
    if status != 200 or not health.get("robot_connected"):
        return record("SETUP", "API is up and a robot is subscribed to /orders/new", False,
                      f"health={health}")

    strays = []
    for item, (sx, sy) in SPAWN.items():
        pose = gz_pose(item)
        if pose is None:
            strays.append(f"{item}: no pose from Gazebo")
        elif math.hypot(pose[0] - sx, pose[1] - sy) > 0.5:
            strays.append(f"{item} is at ({pose[0]:+.2f}, {pose[1]:+.2f}), "
                          f"not on its shelf ({sx:+.2f}, {sy:+.2f})")
    if strays:
        return record("SETUP", "parcels are on their shelves (fresh world)", False,
                      "; ".join(strays) + " - restart with ./scripts/stop.sh && "
                      "./scripts/run.sh --headless")

    return record("SETUP", "API up, robot connected, parcels on their shelves", True,
                  f"database={health['database']}, "
                  f"subscribers={health['orders_new_subscribers']}")


def check_uc5_waypoints():
    """UC5 - the administrator maintains the waypoint registry."""
    status, created = api("/locations", "POST", {"name": "acc_bay", "x": 0.5, "y": -0.5})
    listed = [l["name"] for l in api("/locations")[1]]
    deleted, _ = api("/locations/acc_bay", "DELETE")
    gone = "acc_bay" not in [l["name"] for l in api("/locations")[1]]
    ok = status == 201 and "acc_bay" in listed and deleted == 200 and gone
    return record("UC5", "waypoints can be added, listed and removed", ok,
                  f"create={status}, present={'acc_bay' in listed}, "
                  f"delete={deleted}, removed={gone}")


def check_uc1_uc2_uc4_delivery():
    """UC1 submit, UC2 track, UC4 the full pick-and-place sequence."""
    before = gz_pose("item_1")
    status, order = api("/orders", "POST",
                        {"item": "item_1", "point_a": "shelf_1", "point_b": "delivery_1"})
    if status != 201:
        return record("UC1", "an order can be submitted over HTTP", False,
                      f"POST /orders returned {status}: {order}")
    record("UC1", "an order can be submitted over HTTP", True,
           f"order {order['id']} created, status={order['status']}")

    final = wait_for_order(order["id"])
    tracked = final["status"] in ("completed", "failed")
    record("UC2", "an order can be tracked to a terminal state", tracked,
           f"order {final['id']} -> {final['status']}: {final['detail']}")

    after = gz_pose("item_1")
    _, dropoff = api("/locations")
    target = next(l for l in dropoff if l["name"] == "delivery_1")
    if after is None:
        return record("UC4", "the parcel is physically delivered (Gazebo ground truth)",
                      False, "could not read item_1's pose from Gazebo")
    gap = math.hypot(after[0] - target["x"], after[1] - target["y"])
    moved = before is None or math.hypot(after[0] - before[0], after[1] - before[1])
    ok = final["status"] == "completed" and gap <= DELIVERY_TOLERANCE
    return record("UC4", "the parcel is physically delivered (Gazebo ground truth)", ok,
                  f"item_1 at ({after[0]:+.2f}, {after[1]:+.2f}), "
                  f"{gap:.2f} m from delivery_1, moved {moved:.2f} m")


def check_uc6_analytics():
    """UC6 - aggregate statistics agree with the orders table."""
    _, stats = api("/analytics")
    _, orders = api("/orders")
    completed = sum(1 for o in orders if o["status"] == "completed")
    failed = sum(1 for o in orders if o["status"] == "failed")
    ok = (stats["total_orders"] == len(orders)
          and stats["completed"] == completed
          and stats["failed"] == failed)
    return record("UC6", "analytics agree with the orders table", ok,
                  f"reported {stats['completed']}/{stats['total_orders']} completed, "
                  f"counted {completed}/{len(orders)}; rate={stats['success_rate']}")


def check_nfr1_telemetry_latency():
    """NFR1 - clients see a change within 1 second."""
    try:
        import asyncio

        import websockets
    except ImportError:
        return record("NFR1", "order updates reach a client within 1 s", False,
                      "websockets library not available")

    async def measure():
        async with websockets.connect("ws://localhost:8000/telemetry") as ws:
            await ws.recv()                                   # snapshot
            sent = time.time()
            _, order = api("/orders", "POST", {"item": "item_2", "point_a": "shelf_2",
                                               "point_b": "delivery_2"})
            while time.time() - sent < 5:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if message.get("type") == "order" and message.get("id") == order["id"]:
                    return time.time() - sent, order["id"]
            return None, order["id"]

    latency, order_id = asyncio.run(measure())
    ok = latency is not None and latency < 1.0
    record("NFR1", "order updates reach a client within 1 s", ok,
           f"{latency * 1000:.0f} ms" if latency else "no message within 5 s")
    # Let that order finish so it does not disturb later checks.
    wait_for_order(order_id)
    return ok


def check_nfr2_scheduler_speed():
    """NFR2 - scheduling 20 orders takes well under 100 ms."""
    from robofetch_core.order import Order
    from robofetch_core.scheduler import select_next_order

    orders = [Order(order_id=i, item=f"item_{i}", pickup=(i * 0.3, i * 0.2),
                    dropoff=(-3.0, -2.0)) for i in range(20)]
    start = time.perf_counter()
    select_next_order((0.0, 0.0), orders)
    elapsed_ms = (time.perf_counter() - start) * 1000
    ok = elapsed_ms < 100
    return record("NFR2", "scheduling 20 orders takes under 100 ms", ok,
                  f"{elapsed_ms:.4f} ms for 20 orders")


def check_uc3_scheduler_order():
    """UC3 - the nearest pending pickup is served first, not the submission order."""
    # Submitted worst-first: from the delivery station, shelf_3 is farthest and shelf_1
    # nearest, so a FIFO queue would serve 3,1 and the scheduler should serve 1,3.
    _, far = api("/orders", "POST", {"item": "item_3", "point_a": "shelf_3",
                                     "point_b": "delivery_3"})
    _, near = api("/orders", "POST", {"item": "item_1", "point_a": "shelf_1",
                                      "point_b": "delivery_1"})
    served = []
    deadline = time.time() + 600
    while time.time() < deadline and len(served) < 2:
        for order_id in (far["id"], near["id"]):
            _, row = api(f"/orders/{order_id}")
            if row["status"] not in ("pending",) and order_id not in served:
                served.append(order_id)
        time.sleep(1)

    for order_id in (far["id"], near["id"]):
        wait_for_order(order_id)

    ok = bool(served) and served[0] == near["id"]
    return record("UC3", "nearest-neighbour scheduler serves the closest pickup first", ok,
                  f"submitted [item_3, item_1], started {served} "
                  f"(item_1 is order {near['id']}, item_3 is order {far['id']})")


def check_fr4_retry():
    """FR4 - a grab is retried up to 3 times, then the order fails and the queue goes on."""
    # Point the pickup at empty floor so the parcel is out of the gripper's reach. This is
    # the failure the retry FSM exists for.
    api("/locations", "POST", {"name": "acc_empty", "x": 0.0, "y": 0.0})
    _, order = api("/orders", "POST", {"item": "item_1", "point_a": "acc_empty",
                                       "point_b": "delivery_1"})
    final = wait_for_order(order["id"], timeout=900)
    api("/locations/acc_empty", "DELETE")

    ok = final["status"] == "failed" and final["retries"] == 3
    return record("FR4", "a failing grab is retried 3 times, then the order fails", ok,
                  f"order {final['id']} -> {final['status']} after {final['retries']} "
                  f"attempts: {final['detail']}")


# ----------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true",
                        help="skip the slow checks (UC3 scheduler, FR4 retry)")
    parser.add_argument("--json", metavar="FILE", help="also write results as JSON")
    args = parser.parse_args()

    print("RoboFetch acceptance tests")
    print("=" * 72)

    if not check_system_ready():
        print("\nThe system is not ready. Start it with ./scripts/run.sh --headless and\n"
              "wait for 'Localized and ready' before running this.")
        return 1

    check_uc5_waypoints()
    check_uc1_uc2_uc4_delivery()
    check_nfr1_telemetry_latency()
    check_nfr2_scheduler_speed()
    if not args.quick:
        check_uc3_scheduler_order()
        check_fr4_retry()
    check_uc6_analytics()

    passed = sum(1 for r in results if r["passed"])
    print("=" * 72)
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
