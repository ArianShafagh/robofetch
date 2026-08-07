"""Integration tests for the web tier (proposal §12, Testing phase).

The unit suites cover pure logic - the condition model, the admission policy, the database.
None of them check that the pieces fit together, which is where the expensive bugs have lived:
an order that reaches SQLite but never reaches the robot looks exactly like one that worked.

These cover the SEAMS:

    HTTP request -> admission -> SQLite row -> JSON published on /orders/new
    /orders/status  -> SQLite update -> delivery_history
    /robot/telemetry -> robot_state + the telemetry time-series

ROS and the AI service are both faked, so the whole file runs in about a second with no
simulator and no network. Physical behaviour is covered by scripts/acceptance.py.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


class FakeNode:
    """Stands in for RosLink: records what would have been published to the robot."""

    def __init__(self):
        self.robot_xy = None
        self.telemetry = {}
        self.submitted = []
        self.subscription_count = 1      # tests flip this to simulate "no robot running"

    @property
    def order_pub(self):
        return self

    def get_subscription_count(self):
        return self.subscription_count

    def submit_order(self, order_id, product, pickup, dropoff):
        self.submitted.append({"id": order_id, "product_id": product["product_id"],
                               "model": product["model_name"],
                               "pickup": tuple(pickup), "dropoff": tuple(dropoff)})


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A fresh app with a throwaway database, no ROS and no AI service.

    app.py builds its Database, RosThread and Predictor at import time, so the patches have to
    be in place before the module is (re)loaded - hence the reload rather than a plain import.
    """
    monkeypatch.setenv("ROBOFETCH_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ROBOFETCH_WEB", raising=False)     # REST only; no templates needed

    import robofetch_bridge.ros_link as ros_link
    hooks = {}

    class FakeRosThread:
        def __init__(self, on_status=None, on_telemetry=None):
            self.node = FakeNode()
            hooks["status"] = on_status
            hooks["telemetry"] = on_telemetry

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(ros_link, "RosThread", FakeRosThread)

    import robofetch_bridge.app as app_module
    importlib.reload(app_module)
    # Default to "AI unreachable" so tests exercise the deterministic path unless they opt in.
    app_module.predictor = lambda features: None
    app_module.predictor.status = lambda: {"url": "fake", "reachable": False, "error": None}

    with TestClient(app_module.app) as client:
        yield client, app_module, hooks


def order(client, product="SKU-1002", delivery="delivery_1"):
    return client.post("/api/orders", json={"product_id": product, "delivery_id": delivery})


# ------------------------------------------------------------------- catalogue
def test_catalogue_and_destinations_are_served(api):
    client, _, _ = api
    products = client.get("/api/products").json()
    assert len(products) == 6
    assert all("weight_kg" in p and "pick_x" in p for p in products)
    assert len(client.get("/api/delivery-points").json()) == 2


# --------------------------------------------------------------------- preview
def test_preview_costs_the_job_without_creating_an_order(api):
    client, _, _ = api
    body = client.post("/api/preview",
                       json={"product_id": "SKU-3001", "delivery_id": "delivery_1"}).json()
    assert body["decision"] in ("accepted", "refused")
    assert body["estimate"]["energy_wh"] > 0
    assert body["estimate"]["seconds"] > 0
    assert client.get("/api/orders").json() == []          # nothing was stored


def test_preview_rejects_unknown_products(api):
    client, _, _ = api
    response = client.post("/api/preview",
                           json={"product_id": "SKU-NOPE", "delivery_id": "delivery_1"})
    assert response.status_code == 404


# -------------------------------------------------------------- order admission
def test_accepted_order_reaches_the_database_and_the_robot(api):
    client, app_module, _ = api
    body = order(client, "SKU-1001").json()

    assert body["status"] == "pending"
    assert body["decision"] == "accepted"
    assert body["estimated_energy_wh"] > 0

    submitted = app_module.ros.node.submitted
    assert len(submitted) == 1
    assert submitted[0]["product_id"] == "SKU-1001"
    # The bridge resolves the SKU to its simulator model and pick-point coordinates so the
    # robot side never needs the catalogue.
    assert submitted[0]["model"] == "parcel_1"
    assert submitted[0]["pickup"] == (-3.0, 0.95)
    assert submitted[0]["dropoff"] == (-3.0, -2.2)


def test_refused_order_is_recorded_but_never_sent(api):
    client, app_module, _ = api
    # Flatten the battery so the reserve gate must fire.
    app_module.db.update_robot_state(battery_percent=12.0)

    body = order(client, "SKU-3001").json()
    assert body["status"] == "refused"
    assert "reserve" in body["decision_reason"]
    assert app_module.ros.node.submitted == []             # the robot was never told


def test_order_is_flagged_when_no_robot_is_listening(api):
    """Publishing to a topic with no subscribers succeeds silently - say so instead."""
    client, app_module, _ = api
    app_module.ros.node.subscription_count = 0
    body = order(client).json()
    assert "NOT sent" in body["detail"]


def test_missing_order_is_404(api):
    client, _, _ = api
    assert client.get("/api/orders/999").status_code == 404


def test_cancel_pending_then_conflict(api):
    client, _, _ = api
    created = order(client).json()
    assert client.delete(f"/api/orders/{created['id']}").json()["status"] == "cancelled"
    assert client.delete(f"/api/orders/{created['id']}").status_code == 409


# --------------------------------------------------- status coming back from robot
def test_robot_status_updates_the_order_and_records_history(api):
    client, app_module, hooks = api
    created = order(client).json()

    hooks["status"]({"id": created["id"], "status": "navigating", "attempts": 0,
                     "detail": "heading to the pick point"})
    assert client.get(f"/api/orders/{created['id']}").json()["status"] == "navigating"

    hooks["status"]({"id": created["id"], "status": "completed", "attempts": 1,
                     "detail": "delivered (0.61 m from target)"})

    stored = client.get(f"/api/orders/{created['id']}").json()
    assert stored["status"] == "completed"
    assert stored["attempts"] == 1
    assert stored["completed_at"] is not None

    history = app_module.db.list_history()
    assert len(history) == 1
    assert history[0]["outcome"] == "completed"
    assert history[0]["energy_wh"] is not None


def test_failed_order_is_not_charged_a_full_delivery_of_energy(api):
    client, app_module, hooks = api
    created = order(client).json()
    hooks["status"]({"id": created["id"], "status": "failed", "attempts": 3,
                     "detail": "could not grab it"})
    history = app_module.db.list_history()
    assert history[0]["outcome"] == "failed"
    assert history[0]["energy_wh"] is None


# ------------------------------------------------------------------- telemetry
def test_telemetry_updates_current_state_and_the_time_series(api):
    client, app_module, hooks = api

    hooks["telemetry"]({"battery_percent": 71.5, "temperature_c": 48.0,
                        "condition_percent": 96.0, "payload_kg": 1.8,
                        "state": "working", "speed": 0.2})

    current = client.get("/api/robot").json()
    assert current["battery_percent"] == pytest.approx(71.5)
    assert current["state"] == "working"
    assert len(app_module.db.list_telemetry()) == 1


def test_low_battery_telemetry_starts_getting_orders_refused(api):
    """The end-to-end point of the condition model: it must actually change decisions."""
    client, _, hooks = api

    assert order(client, "SKU-3001").json()["decision"] == "accepted"

    hooks["telemetry"]({"battery_percent": 10.0, "temperature_c": 22.0,
                        "condition_percent": 100.0, "state": "working"})

    refused = order(client, "SKU-3001").json()
    assert refused["status"] == "refused"


# ------------------------------------------------------------------- analytics
def test_analytics_agree_with_the_orders_table(api):
    client, _, hooks = api
    first, second = order(client).json(), order(client, "SKU-2002").json()
    hooks["status"]({"id": first["id"], "status": "completed", "attempts": 0, "detail": ""})
    hooks["status"]({"id": second["id"], "status": "failed", "attempts": 3, "detail": "x"})

    stats = client.get("/api/analytics").json()
    assert stats["total_orders"] == 2
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["success_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------- health
def test_health_reports_the_robot_link(api):
    client, app_module, _ = api
    assert client.get("/health").json()["robot_connected"] is True

    app_module.ros.node.subscription_count = 0
    body = client.get("/health").json()
    assert body["robot_connected"] is False
    assert body["orders_new_subscribers"] == 0
