"""Integration tests for the web tier (proposal: testing section, M9).

The existing 50 unit tests all exercise PURE logic - the scheduler, the retry state
machine, and the database in isolation. None of them check that the pieces actually fit
together, which is where the interesting bugs turned out to live: an order that reaches
SQLite but never reaches the robot looks identical to one that worked (HANDOVER 5.9).

These tests therefore cover the SEAMS:

    HTTP request  ->  SQLite row  ->  JSON published on /orders/new
    /orders/status message  ->  SQLite update  ->  delivery_history  ->  WebSocket push

ROS is replaced by a fake, so they need no Gazebo, no Nav2 and no network: the whole file
runs in well under a second and is deterministic. Physical behaviour is covered separately
by the acceptance tests in scripts/acceptance.py, which do drive the real simulator.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


class FakeNode:
    """Stands in for RosLink: records what would have been published to the robot."""

    def __init__(self):
        self.robot_pose = {"x": 0.0, "y": 0.0}
        self.item_poses = {}
        self.submitted = []
        self.subscription_count = 1      # tests flip this to simulate "no robot running"

    # app.py calls ros.node.order_pub.get_subscription_count(); the fake is its own pub.
    @property
    def order_pub(self):
        return self

    def get_subscription_count(self):
        return self.subscription_count

    def submit_order(self, order_id, item, pickup, dropoff):
        self.submitted.append({"id": order_id, "item": item,
                               "pickup": tuple(pickup), "dropoff": tuple(dropoff)})


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A fresh app instance with a throwaway database and no real ROS.

    app.py builds its Database and RosThread at import time, so the patch has to be in
    place before the module is (re)loaded - hence the reload rather than a plain import.
    """
    monkeypatch.setenv("ROBOFETCH_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ROBOFETCH_WEB", raising=False)   # no static mount during tests

    import robofetch_bridge.ros_link as ros_link

    hooks = {}

    class FakeRosThread:
        def __init__(self, on_status=None, on_pose=None, on_item=None):
            self.node = FakeNode()
            hooks["status"] = on_status
            hooks["pose"] = on_pose
            hooks["item"] = on_item

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(ros_link, "RosThread", FakeRosThread)

    import robofetch_bridge.app as app_module
    importlib.reload(app_module)

    with TestClient(app_module.app) as client:
        yield client, app_module, hooks


def submit(client, item="item_1", a="shelf_1", b="delivery_1"):
    return client.post("/orders", json={"item": item, "point_a": a, "point_b": b})


# --------------------------------------------------------------- order submission
def test_order_reaches_both_the_database_and_the_robot(api):
    client, app_module, _ = api

    response = submit(client)
    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "pending"

    # It is in SQLite ...
    assert client.get(f"/orders/{order['id']}").json()["item"] == "item_1"

    # ... and it was handed to the robot with the waypoint NAMES resolved to coordinates,
    # which is the bridge's real job - the robot side only ever deals in numbers.
    submitted = app_module.ros.node.submitted
    assert len(submitted) == 1
    assert submitted[0]["id"] == order["id"]
    assert submitted[0]["pickup"] == (-2.5, 0.95)       # shelf_1 from DEFAULT_LOCATIONS
    assert submitted[0]["dropoff"] == (-3.1, -2.2)      # delivery_1


def test_unknown_waypoint_is_rejected_before_anything_is_stored(api):
    client, app_module, _ = api

    response = submit(client, a="nowhere")
    assert response.status_code == 400
    assert "nowhere" in response.json()["detail"]

    assert client.get("/orders").json() == []
    assert app_module.ros.node.submitted == []


def test_order_is_flagged_when_no_robot_is_listening(api):
    """The failure that used to be invisible: publishing to nobody succeeds silently."""
    client, app_module, _ = api
    app_module.ros.node.subscription_count = 0

    order = submit(client).json()

    # The database is still the record of what was ASKED FOR, so the order is kept ...
    assert order["status"] == "pending"
    # ... but it says plainly that it went nowhere.
    assert "NOT sent" in order["detail"]


def test_missing_order_is_404(api):
    client, _, _ = api
    assert client.get("/orders/999").status_code == 404


# ------------------------------------------------------- status coming back from robot
def test_robot_status_updates_the_order_and_records_history(api):
    client, app_module, hooks = api
    order = submit(client).json()

    hooks["status"]({"id": order["id"], "status": "navigating",
                     "retries": 0, "detail": "heading to pickup"})
    assert client.get(f"/orders/{order['id']}").json()["status"] == "navigating"

    hooks["status"]({"id": order["id"], "status": "completed",
                     "retries": 1, "detail": "delivered (0.61 m from target)"})

    stored = client.get(f"/orders/{order['id']}").json()
    assert stored["status"] == "completed"
    assert stored["retries"] == 1
    assert stored["completed_at"] is not None

    # A finished order must also land in delivery_history, which is what /analytics
    # averages over.
    history = app_module.db.list_history()
    assert len(history) == 1
    assert history[0]["order_id"] == order["id"]
    assert history[0]["outcome"] == "completed"
    assert history[0]["duration"] >= 0


def test_pose_updates_are_persisted_to_robot_state(api):
    client, app_module, hooks = api

    hooks["pose"]({"x": 1.25, "y": -0.5})

    stored = app_module.db.get_robot_state()
    assert stored["x"] == pytest.approx(1.25)
    assert stored["y"] == pytest.approx(-0.5)


def test_robot_endpoint_prefers_the_live_pose_over_the_stored_one(api):
    """/robot deliberately overrides the database with the node's freshest AMCL pose.

    The stored value is only as recent as the last message the bridge happened to persist,
    so the endpoint layers the live one on top. Pinned here because it is surprising: a
    write to robot_state is NOT what /robot reads back.
    """
    client, app_module, hooks = api

    hooks["pose"]({"x": 1.25, "y": -0.5})            # persisted to SQLite
    app_module.ros.node.robot_pose = {"x": 3.0, "y": 2.0}   # newer, still in memory

    state = client.get("/robot").json()
    assert state["x"] == pytest.approx(3.0)
    assert state["y"] == pytest.approx(2.0)


# ------------------------------------------------------------------------ cancelling
def test_cancel_pending_then_conflict(api):
    client, _, _ = api
    order = submit(client).json()

    assert client.delete(f"/orders/{order['id']}").json()["status"] == "cancelled"
    # Cancelling twice is a conflict, not a second success.
    assert client.delete(f"/orders/{order['id']}").status_code == 409


# ------------------------------------------------------------------------- locations
def test_location_registry_crud(api):
    client, _, _ = api

    names = [l["name"] for l in client.get("/locations").json()]
    assert {"shelf_1", "shelf_2", "shelf_3", "delivery_1"} <= set(names)

    assert client.post("/locations", json={"name": "bay_9", "x": 1.0, "y": 2.0}).status_code == 201
    # An order can now be routed to the new waypoint - the point of making it editable.
    assert submit(client, b="bay_9").status_code == 201

    assert client.delete("/locations/bay_9").status_code == 200
    assert client.delete("/locations/bay_9").status_code == 404


# ------------------------------------------------------------------------- analytics
def test_analytics_counts_and_success_rate(api):
    client, _, hooks = api
    first, second, third = submit(client).json(), submit(client).json(), submit(client).json()

    hooks["status"]({"id": first["id"], "status": "completed", "retries": 0, "detail": ""})
    hooks["status"]({"id": second["id"], "status": "failed", "retries": 3, "detail": "gave up"})

    stats = client.get("/analytics").json()
    assert stats["total_orders"] == 3
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["pending"] == 1
    # Rate is over FINISHED orders only: the queued one must not drag it down.
    assert stats["success_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------- health
def test_health_reports_whether_a_robot_is_connected(api):
    client, app_module, _ = api

    assert client.get("/health").json()["robot_connected"] is True

    app_module.ros.node.subscription_count = 0
    body = client.get("/health").json()
    assert body["robot_connected"] is False
    assert body["orders_new_subscribers"] == 0


# ------------------------------------------------------------------------- telemetry
def test_websocket_snapshot_contains_everything_the_dashboard_needs(api):
    client, app_module, _ = api
    submit(client)
    app_module.ros.node.item_poses = {"item_1": {"x": -2.5, "y": 1.35}}

    with client.websocket_connect("/telemetry") as ws:
        snapshot = ws.receive_json()

    assert snapshot["type"] == "snapshot"
    assert {"orders", "robot", "locations", "items"} <= set(snapshot)
    assert len(snapshot["orders"]) == 1
    assert snapshot["items"]["item_1"]["x"] == pytest.approx(-2.5)


def test_order_changes_are_pushed_to_connected_clients(api):
    client, _, hooks = api
    order = submit(client).json()

    with client.websocket_connect("/telemetry") as ws:
        ws.receive_json()                                    # the snapshot
        hooks["status"]({"id": order["id"], "status": "navigating",
                         "retries": 0, "detail": "heading to pickup"})
        pushed = ws.receive_json()

    assert pushed["type"] == "order"
    assert pushed["id"] == order["id"]
    assert pushed["status"] == "navigating"
