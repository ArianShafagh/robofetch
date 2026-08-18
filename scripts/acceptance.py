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
import http.cookiejar
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "http://localhost:8000"
WORLD = "warehouse"
# Was 1.1 m, which was all the old grab geometry could promise. The robot now faces the shelf
# when it parks, refuses anything outside a 60-degree window in front, and stops a carry-offset
# short of the bay. Measured across three deliveries: 0.24, 0.31 and 0.49 m - so 0.7 keeps a
# real margin over the worst of them rather than sitting right on top of it.
DELIVERY_TOLERANCE = 0.7
# The pack is deliberately small - about three deliveries - so that admission control has
# something real to refuse. A suite that runs five deliveries back to back therefore WILL run it
# flat, and every later check would fail on a refusal that is the system working correctly. So
# the physical checks wait for the robot to charge first, using the duty cycle rather than
# faking the battery: it drives home when idle and charges there, exactly as in normal use.
CHARGE_TARGET = 85.0

# The HTML pages are behind a login, so the suite has to sign in. Same defaults the database
# seeds; override here if you changed them before first launch.
ADMIN_PASSWORD = os.environ.get("ROBOFETCH_ADMIN_PASSWORD", "admin")
CONTROLLER_PASSWORD = os.environ.get("ROBOFETCH_CONTROLLER_PASSWORD", "controller")

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


def gz_pose(model, attempts=4):
    """The model's real (x, y) in Gazebo, or None.

    Retries, because `dynamic_pose/info` publishes a CHANGED subset each message rather than
    every model every time - so a single sample legitimately may not mention the model asked
    about. One sample returning nothing means "not in that message", not "not in the world".
    """
    for attempt in range(attempts):
        found = _gz_pose_once(model)
        if found is not None:
            return found
        time.sleep(0.5)
    return None


def _gz_pose_once(model):
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


def robot_state():
    _, robot = api("/api/robot")
    return robot or {}


def wait_for_charge(minimum=CHARGE_TARGET, timeout=600):
    """Block until the robot has at least `minimum` percent, or the timeout runs out.

    Returns immediately when it already does, so this costs nothing early in the suite. When it
    does have to wait, it is waiting for FR11 to happen on its own: an empty queue for
    `idle_return_seconds`, then the drive home, then charging at the station.
    """
    deadline = time.time() + timeout
    robot = robot_state()
    while time.time() < deadline:
        if (robot.get("battery_percent") or 0.0) >= minimum:
            return robot
        time.sleep(5)
        robot = robot_state()
    print(f"         (still at {robot.get('battery_percent')}% after waiting to charge)")
    return robot


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
    """UC5/FR1/FR14 - the administrator maintains the catalogue and the layout.

    `?all=true` on purpose: the default view is what a client may order, which shrinks as
    parcels are delivered. The administrator's view is the whole catalogue.
    """
    _, products = api("/api/products?all=true")
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

    # C2's reservation ledger, measured WHILE that order is in flight so it costs no extra
    # delivery: the next job must be costed against the battery the robot will have when this
    # one finishes, not the battery telemetry is reporting right now.
    live = robot_state().get("battery_percent") or 0.0
    _, preview = api("/api/preview", "POST",
                     {"product_id": "SKU-2001", "delivery_id": "delivery_2"})
    booked = (preview or {}).get("estimate", {})
    projected = booked.get("battery_at_start_percent")
    record("C2", "work in flight is reserved against the next admission decision",
           bool(booked.get("reserved_orders")) and projected is not None and projected < live,
           f"robot reports {live:.1f}%, next order costed from {projected}% with "
           f"{booked.get('reserved_orders')} order(s) holding "
           f"{booked.get('reserved_energy_wh')} Wh")

    final = wait_for_order(order["id"])
    record("UC2", "an order can be tracked to a terminal state", final["status"] == "completed",
           f"order {final['id']} -> {final['status']}: {final['detail']}")

    # ... and handed back when the order ends, or the robot would slowly talk itself out of
    # every remaining job.
    _, after = api("/api/preview", "POST",
                   {"product_id": "SKU-2001", "delivery_id": "delivery_2"})
    released = (after or {}).get("estimate", {}).get("reserved_orders")
    record("C2", "the reservation is released when the order finishes", released == 0,
           f"{released} order(s) still reserved after order {final['id']} ended")

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


def check_fr3_delivered_is_no_longer_orderable():
    """FR3 - a delivered product leaves the catalogue of things that can be ordered.

    Runs straight after the UC4 delivery, so SKU-1001's parcel is provably at the bay rather
    than on its shelf. Ordering it again would send the robot to an empty pick point to fail
    three grab attempts, which is not a refusal - it is a wasted trip.
    """
    _, available = api("/api/products")
    _, everything = api("/api/products?all=true")
    gone = "SKU-1001" not in {p["product_id"] for p in available}

    _, order = api("/api/orders", "POST",
                   {"product_id": "SKU-1001", "delivery_id": "delivery_1"})
    refused = bool(order) and order.get("status") == "refused"

    return record("FR3", "a delivered product is no longer orderable",
                  gone and refused and len(everything) == 6,
                  f"{len(available)} of {len(everything)} products orderable; re-ordering "
                  f"SKU-1001 -> {(order or {}).get('status')} "
                  f"('{(order or {}).get('decision_reason')}')")


def check_fr11_idle_return():
    """FR11 - with nothing to do the robot drives home and charges.

    This is what makes the small battery workable: the robot recovers on its own. It is also
    what the rest of this suite depends on, since several deliveries in a row would otherwise
    run the pack flat and later checks would fail on a correct refusal.

    Waits for the robot to DOCK, not for a battery level: the two are different conditions, and
    a battery that happens to be high already says nothing about whether the robot went home.
    Expect this to take the full `idle_return_seconds` plus the drive.
    """
    before = robot_state()
    deadline = time.time() + 420
    docked = before
    while time.time() < deadline and docked.get("state") != "charging":
        time.sleep(5)
        docked = robot_state()

    if docked.get("state") != "charging":
        return record("FR11", "an idle robot returns to its station and recharges", False,
                      f"still {docked.get('state')} at "
                      f"{docked.get('battery_percent'):.1f}% after waiting to dock")

    # Now measure the charge going IN, which is the half of FR11 the condition model provides.
    start = docked.get("battery_percent") or 0.0
    time.sleep(15)
    after = robot_state()
    now = after.get("battery_percent") or 0.0
    rising = now > start or start >= 99.9
    return record("FR11", "an idle robot returns to its station and recharges", rising,
                  f"{before.get('state')} at {before.get('battery_percent'):.1f}% -> docked "
                  f"and charging, {start:.1f}% -> {now:.1f}%")


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
    _, product = api("/api/products?all=true")
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
    if stats is None or orders is None:
        return record("UC6", "analytics agree with the orders table", False,
                      "the API did not answer - is it still running?")
    completed = sum(1 for o in orders if o["status"] == "completed")
    refused = sum(1 for o in orders if o["status"] == "refused")
    ok = (stats["total_orders"] == len(orders) and stats["completed"] == completed
          and stats["refused"] == refused)
    return record("UC6", "analytics agree with the orders table", ok,
                  f"reported {stats['completed']}/{stats['total_orders']} completed and "
                  f"{stats['refused']} refused; counted {completed} and {refused}; "
                  f"rate={stats['success_rate']}")


def browser(username, password):
    """A urllib opener holding a session cookie, i.e. a signed-in browser.

    The HTML pages are behind a login now, so checking them means logging in first. Returns
    (opener, error) - the opener still works for anonymous requests if the login failed, which is
    what lets the gate itself be tested.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode({"username": username, "password": password,
                                   "next": "/"}).encode()
    try:
        opener.open(f"{API}/login", body, timeout=10).read()
    except Exception as exc:                                       # noqa: BLE001
        return opener, str(exc)
    signed_in = any(c.name == "robofetch_session" for c in jar)
    return opener, None if signed_in else "no session cookie was set"


def fetch(opener, path):
    """(status, body) for a page, or (error string, None)."""
    try:
        with opener.open(f"{API}{path}", timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception as exc:                                       # noqa: BLE001
        return str(exc), None


def check_uc12_roles():
    """UC12 - the pages require a sign-in, and the two roles differ.

    Three separate claims, because a broken one of them fails silently: anonymous visitors are
    turned away, a controller cannot reach the admin section, and an admin can.
    """
    anon = urllib.request.build_opener(NoRedirect())
    status, _ = fetch(anon, "/robot")
    record("UC12", "an anonymous visitor is sent to the login page", status == 303,
           f"GET /robot while signed out -> {status} (expected 303 redirect)")

    controller, error = browser("controller", CONTROLLER_PASSWORD)
    if error:
        return record("UC12", "the two roles have different access", False,
                      f"could not sign in as controller: {error}")
    c_robot, _ = fetch(controller, "/robot")
    c_admin, _ = fetch(controller, "/admin")
    record("UC12", "a controller can drive the robot but not administer it",
           c_robot == 200 and c_admin == 403,
           f"controller: /robot -> {c_robot}, /admin -> {c_admin} (expected 200, 403)")

    admin, error = browser("admin", ADMIN_PASSWORD)
    if error:
        return record("UC12", "an administrator reaches the admin section", False,
                      f"could not sign in as admin: {error}")
    a_admin, _ = fetch(admin, "/admin")
    a_db, body = fetch(admin, "/admin/db?table=products")
    return record("UC12", "an administrator reaches the admin section and the database",
                  a_admin == 200 and a_db == 200 and "SKU-1001" in (body or ""),
                  f"admin: /admin -> {a_admin}, /admin/db?table=products -> {a_db}")


def check_uc13_admin_crud():
    """UC13/FR1 - the administrator maintains the catalogue through the web app."""
    admin, error = browser("admin", ADMIN_PASSWORD)
    if error:
        return record("UC13", "an administrator can create, update and delete a product", False,
                      f"could not sign in as admin: {error}")

    def post(path, fields):
        try:
            return admin.open(f"{API}{path}", urllib.parse.urlencode(fields).encode(),
                              timeout=10).status
        except urllib.error.HTTPError as exc:
            return exc.code

    fields = {"product_id": "SKU-CRUD", "name": "Acceptance widget", "category": "test",
              "weight_kg": "1.4", "stock": "1", "shelf_id": "shelf_1",
              "pick_point_id": "pick_1a", "model_name": "parcel_1"}
    post("/admin/products", fields)
    _, created = api("/api/products?all=true")
    made = next((p for p in created if p["product_id"] == "SKU-CRUD"), None)

    post("/admin/products", dict(fields, name="Renamed widget", weight_kg="2.6"))
    _, updated_all = api("/api/products?all=true")
    updated = next((p for p in updated_all if p["product_id"] == "SKU-CRUD"), None)

    post("/admin/products/delete", {"product_id": "SKU-CRUD"})
    _, after = api("/api/products?all=true")
    gone = not any(p["product_id"] == "SKU-CRUD" for p in after)

    ok = (made is not None and updated is not None
          and updated["name"] == "Renamed widget" and updated["weight_kg"] == 2.6 and gone)
    return record("UC13", "an administrator can create, update and delete a product", ok,
                  f"created={made is not None}, "
                  f"updated={(updated or {}).get('name')} @ {(updated or {}).get('weight_kg')} kg, "
                  f"deleted={gone}")


def check_uc11_emergency_stop():
    """UC11 - the stop abandons all work and halts the robot, and leaves nothing engaged.

    Run LAST among the physical checks: it interrupts a delivery on purpose. Verified against
    Gazebo - the robot's own pose must stop changing - because "the API said stopped" is a claim,
    not evidence.
    """
    _, order = api("/api/orders", "POST",
                   {"product_id": "SKU-2001", "delivery_id": "delivery_2"})
    if not order or order.get("status") == "refused":
        return record("UC11", "the emergency stop halts the robot", False,
                      f"could not start an order to interrupt: {order}")

    # Let it actually get moving, or "it stopped" proves nothing.
    deadline = time.time() + 180
    while time.time() < deadline:
        _, row = api(f"/api/orders/{order['id']}")
        if row and row["status"] in ("navigating", "grabbing", "delivering"):
            break
        time.sleep(2)
    time.sleep(8)

    status, body = api("/api/estop", "POST")
    time.sleep(6)
    settled = gz_pose("robofetch")
    time.sleep(5)
    after = gz_pose("robofetch")

    if settled is None or after is None:
        return record("UC11", "the emergency stop halts the robot", False,
                      "could not read the robot's pose from Gazebo")
    drift = math.hypot(after[0] - settled[0], after[1] - settled[1])
    record("UC11", "the emergency stop halts the robot (Gazebo ground truth)",
           bool(body and body.get("robot_listening")) and drift < 0.05,
           f"robot moved {drift:.3f} m in the 5 s after it settled; "
           f"listening={(body or {}).get('robot_listening')}")

    final = wait_for_order(order["id"], timeout=120)
    record("UC11", "the interrupted order is failed, not left pending",
           bool(final) and final["status"] == "failed"
           and "emergency stop" in (final["detail"] or ""),
           f"order {order['id']} -> {(final or {}).get('status')}: {(final or {}).get('detail')}")

    # One shot: nothing is engaged afterwards, so the very next order is an ordinary order.
    # Asserted through behaviour rather than through the absence of a column, because that is
    # what the operator experiences and it cannot be fooled by leftover schema.
    _, resumed = api("/api/orders", "POST",
                     {"product_id": "SKU-3001", "delivery_id": "delivery_1"})
    accepted = bool(resumed) and resumed.get("decision") == "accepted"
    record("UC11", "nothing stays engaged - the robot accepts work immediately after",
           accepted,
           f"next order -> {(resumed or {}).get('decision')}: "
           f"'{(resumed or {}).get('decision_reason')}'")
    left_engaged = False

    # Tidy up so later checks are not left with a delivery in flight.
    if accepted and resumed.get("id"):
        api("/api/estop", "POST")
        wait_for_order(resumed["id"], timeout=120)
    return accepted and not left_engaged


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report the redirect instead of following it, so the login gate is observable."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_nfr3_pages():
    """NFR3 - every page renders server-side, with no JavaScript."""
    admin, error = browser("admin", ADMIN_PASSWORD)
    if error:
        return record("NFR3", "all pages render server-side with no JavaScript", False,
                      f"could not sign in: {error}")
    pages = {}
    for path in ("/", "/orders", "/robot", "/login", "/admin", "/admin/products",
                 "/admin/delivery-points", "/admin/db"):
        status, body = fetch(admin, path)
        pages[path] = (status, "<script" in (body or "").lower())
    ok = all(status == 200 and not has_script for status, has_script in pages.values())
    return record("NFR3", "all pages render server-side with no JavaScript", ok,
                  f"{len(pages)} pages checked: "
                  + ", ".join(f"{p} -> {s[0]}{' (has <script>!)' if s[1] else ''}"
                              for p, s in pages.items() if s[0] != 200 or s[1])
                  or f"{len(pages)} pages all 200, none contain <script>")


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
    check_uc12_roles()
    check_uc13_admin_crud()
    check_uc9_preview()
    check_nfr3_pages()
    check_uc1_uc2_uc4_delivery()
    check_fr3_delivered_is_no_longer_orderable()
    check_fr10_fr12_telemetry()
    check_fr5_refusal()
    # From here on the checks need charge. The pack holds about three deliveries, so waiting
    # for the robot to dock and recharge is part of the suite, not a workaround for it.
    check_fr11_idle_return()
    if not args.quick:
        check_fr6_fifo()
        wait_for_charge()
        check_fr8_retry()
        # Last of the physical checks on purpose: it halts the robot mid-drive, and although it
        # clears the stop afterwards, anything running after it starts from a stopped robot.
        wait_for_charge()
        check_uc11_emergency_stop()
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
