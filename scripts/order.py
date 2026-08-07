#!/usr/bin/env python3
"""Order a product and follow it until it finishes.

    ./scripts/order.sh                        # SKU-1001 -> delivery_1
    ./scripts/order.sh SKU-3001 delivery_2
    ./scripts/order.sh --list                 # catalogue and destinations

Why this exists instead of a bare curl: every way this system can fail still returns valid
JSON, so "the order was accepted" and "the order will never run" look identical from a second
terminal. This checks the real failure modes BEFORE submitting, shows the admission verdict
with its reasoning, prints every state change, and finally compares the parcel's REAL position
in Gazebo against the delivery bay - because a status of `completed` is a claim and the world
pose is the evidence.

Written in Python rather than shell because the shell version needed JSON inside single quotes
inside double quotes, and the quoting was more error-prone than the logic.
"""
import argparse
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "localhost:8000"
WORLD = "warehouse"
DELIVERED_TOLERANCE_M = 1.1


def api(path, method="GET", body=None, timeout=10):
    request = urllib.request.Request(
        f"http://{API}{path}", method=method,
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
            # position{} precedes orientation{}, so the first x:/y: after the name are ours.
            x = y = None
            for follow in lines[index + 1:index + 12]:
                stripped = follow.strip()
                if x is None and stripped.startswith("x:"):
                    x = float(stripped.split()[1])
                elif x is not None and y is None and stripped.startswith("y:"):
                    return x, float(stripped.split()[1])
    return None


def show_catalogue():
    _, products = api("/api/products")
    print("Products:")
    for p in products or []:
        print(f"  {p['product_id']:10} {p['name'][:30]:32} {p['weight_kg']:>4} kg  "
              f"{p['shelf_id']}")
    _, points = api("/api/delivery-points")
    print("Destinations:")
    for d in points or []:
        print(f"  {d['delivery_id']:12} {d['name']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("product", nargs="?", default="SKU-1001")
    parser.add_argument("delivery", nargs="?", default="delivery_1")
    parser.add_argument("--list", action="store_true", help="show the catalogue and exit")
    args = parser.parse_args()

    if args.list:
        show_catalogue()
        return 0

    # ------------------------------------------------------------ pre-flight
    print(f"[order] checking the API on {API} ...")
    status, health = api("/health", timeout=5)
    if health is None:
        print("[order] ERROR: nothing is answering on", API)
        print("[order]   The stack is probably still starting. Wait for this line in the")
        print("[order]   launch terminal: 'Localized and ready - orders submitted now will")
        print("[order]   execute.' If it has finished starting, check the log for")
        print("[order]   'address already in use'.")
        return 1

    print(f"[order]   database        : {health.get('database')}")
    print(f"[order]   robot connected : {health.get('robot_connected')}")

    if not health.get("robot_connected"):
        print()
        print("[order] ERROR: the API is running but NO ROBOT is subscribed to /orders/new.")
        print("[order]   An order submitted now would be stored and never executed. This is")
        print("[order]   usually a STALE api holding port 8000, so the real launch's API died")
        print("[order]   with 'address already in use'. If the database path above is not")
        print("[order]   ~/robofetch_ws/robofetch.db you are talking to the wrong process:")
        print()
        print("      ./scripts/stop.sh && ./scripts/run.sh --headless")
        print()
        return 1

    # --------------------------------------------------------------- verdict
    print(f"[order] asking whether the robot will take {args.product} to {args.delivery} ...")
    status, preview = api("/api/preview", "POST",
                          {"product_id": args.product, "delivery_id": args.delivery})
    if status != 200:
        print(f"[order] ERROR: {(preview or {}).get('detail', 'unknown product or destination')}")
        show_catalogue()
        return 1

    product, estimate = preview["product"], preview["estimate"]
    print(f"  product : {product['product_id']} - {product['name']} "
          f"({product['weight_kg']} kg)")
    print(f"  route   : {estimate['distance_m']} m "
          f"+ {estimate['return_distance_m']} m back to the station")
    print(f"  cost    : {estimate['energy_wh']} Wh, ~{estimate['seconds']:.0f} s")
    print(f"  after   : battery {estimate['battery_after_percent']}%, "
          f"peak {estimate['peak_temperature_c']} C")
    print(f"  verdict : {preview['decision'].upper()} "
          f"(decided by {preview['decided_by']})")
    print(f"  reason  : {preview['reason']}")

    _, health = api("/health", timeout=5)
    ai = health.get("ai_service", {})
    state = {True: "reachable", False: "unreachable", None: "not consulted"}[ai.get("reachable")]
    print(f"  AI      : {state} ({ai.get('url')})")

    if preview["decision"] != "accepted":
        print()
        print("[order] The robot refuses this order, so nothing was submitted.")
        print("[order] Try again once it has charged or cooled down - see /robot.")
        return 2

    # ---------------------------------------------------------------- submit
    status, created = api("/api/orders", "POST",
                          {"product_id": args.product, "delivery_id": args.delivery})
    if created is None or "id" not in created:
        print("[order] ERROR: the API rejected the order.")
        return 1
    order_id = created["id"]
    print(f"\n[order] submitted as order {order_id}\n")

    # ------------------------------------------------------------- follow it
    last = None
    pending_for = 0
    final = created
    for _ in range(240):
        _, order = api(f"/api/orders/{order_id}", timeout=5)
        if order is None:
            print("[order] lost contact with the API.")
            return 1
        final = order
        marker = (order["status"], order.get("detail") or "")
        if marker != last:
            print(f"  {order['status']:<12} {order.get('detail') or ''}")
            last = marker
        if order["status"] in ("completed", "failed", "cancelled", "refused"):
            break
        if order["status"] == "pending":
            pending_for += 2
            if pending_for == 40:
                print("  ... still pending after 40 s. The robot is most likely not localized")
                print("      yet. Look for 'Localized and ready' in the launch terminal. Do")
                print("      NOT trust 'ros2 topic echo /amcl_pose' - it is latched and")
                print("      answers even when AMCL has been silent for minutes.")
        time.sleep(2)

    print(f"\n[order] final status: {final['status']}")
    print(f"[order] detail       : {final.get('detail') or ''}")

    # ------------------------------------------- verify against Gazebo truth
    print("\n[order] verifying against Gazebo ground truth ...")
    model = product["model_name"]
    pose = gz_pose(model)
    if pose is None:
        print(f"[order]   could not read {model}'s pose from Gazebo (is the sim running?)")
        return 0

    _, points = api("/api/delivery-points")
    target = next((d for d in points if d["delivery_id"] == args.delivery), None)
    gap = math.hypot(pose[0] - target["x"], pose[1] - target["y"])

    print(f"[order]   {model} ({args.product}) is really at ({pose[0]:+.2f}, {pose[1]:+.2f})")
    print(f"[order]   {args.delivery} is at{'':12}({target['x']:+.2f}, {target['y']:+.2f})")
    print(f"[order]   distance from the bay: {gap:.2f} m")
    print()
    if gap <= DELIVERED_TOLERANCE_M:
        print("[order]   VERIFIED: the parcel really was delivered.")
        return 0
    print("[order]   NOT DELIVERED: the parcel is not at the delivery bay.")
    print("[order]   If the status above says completed, the software and physical reality")
    print("[order]   disagree - which is the bug the ground-truth check exists to catch.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
