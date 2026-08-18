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
        self.returned = []
        self.estops = []
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

    def submit_return(self, order_id, station):
        self.returned.append({"id": order_id, "station": (station["x"], station["y"])})

    @property
    def estop_pub(self):
        return self

    def publish_estop(self, action):
        self.estops.append(action)
        return self.subscription_count


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


# ------------------------------------------------ stock, reservation, restart
def test_a_delivered_product_stops_being_orderable(api):
    """Its parcel is at the bay now. Ordering it again would send the robot to a bare shelf."""
    client, _, hooks = api
    created = order(client, "SKU-1001").json()
    hooks["status"]({"id": created["id"], "status": "completed", "attempts": 0,
                     "detail": "delivered"})

    available = {p["product_id"] for p in client.get("/api/products").json()}
    assert "SKU-1001" not in available
    assert len(available) == 5
    # The full catalogue is still reachable for an administrator.
    assert len(client.get("/api/products?all=true").json()) == 6

    again = order(client, "SKU-1001").json()
    assert again["status"] == "refused"
    assert "out of stock" in again["decision_reason"]


def test_a_failed_delivery_leaves_the_product_orderable(api):
    """The parcel never left its shelf, so nothing about the catalogue changed."""
    client, _, hooks = api
    created = order(client, "SKU-1001").json()
    hooks["status"]({"id": created["id"], "status": "failed", "attempts": 3,
                     "detail": "could not grab it"})
    assert "SKU-1001" in {p["product_id"] for p in client.get("/api/products").json()}


def test_orders_in_flight_are_held_against_the_next_one(api):
    """C2's reservation ledger, end to end: the queue spends the battery before the next job."""
    client, app_module, hooks = api
    app_module.db.update_robot_state(battery_percent=62.0, temperature_c=22.0,
                                     condition_percent=100.0)

    first = order(client, "SKU-3001", "delivery_1").json()
    assert first["decision"] == "accepted"

    # Nothing has moved yet - the robot still reports 62% - but that charge is spoken for.
    assert app_module.db.get_robot_state()["battery_percent"] == pytest.approx(62.0)
    second = order(client, "SKU-1001", "delivery_1").json()
    assert second["status"] == "refused"
    assert "already in progress" in second["decision_reason"]

    # Finishing the first order hands its reservation back, and the next order fits again.
    hooks["status"]({"id": first["id"], "status": "completed", "attempts": 0, "detail": "done"})
    assert app_module.db.committed_load()["orders"] == 0
    assert order(client, "SKU-1001", "delivery_1").json()["decision"] == "accepted"


def test_a_restart_clears_the_history_and_restocks_the_shelves(api):
    """Every launch restores the same Gazebo world, so the database has to match it."""
    client, app_module, hooks = api
    created = order(client, "SKU-1001").json()
    hooks["status"]({"id": created["id"], "status": "completed", "attempts": 0,
                     "detail": "delivered"})
    assert client.get("/api/orders").json() != []

    app_module.db.reset_session()               # what the startup event does

    assert client.get("/api/orders").json() == []
    assert client.get("/api/analytics").json()["total_orders"] == 0
    assert len(client.get("/api/products").json()) == 6
    assert order(client, "SKU-1001").json()["decision"] == "accepted"


# ---------------------------------------------------------- return to station
def test_return_is_queued_and_sent_to_the_robot(api):
    client, app_module, _ = api
    body = client.post("/api/orders/return").json()

    assert body["kind"] == "return"
    assert body["status"] == "pending"
    assert body["decision"] == "accepted"
    assert app_module.ros.node.returned == [{"id": body["id"], "station": (0.0, -2.2)}]


def test_a_return_is_never_refused_however_bad_the_robot_is(api):
    """Admission control must not stand between a struggling robot and its charger."""
    client, app_module, _ = api
    app_module.db.update_robot_state(battery_percent=2.0, temperature_c=69.0,
                                     condition_percent=5.0)
    body = client.post("/api/orders/return").json()
    assert body["status"] == "pending"
    assert body["decision"] == "accepted"


def test_a_second_return_conflicts_while_the_first_is_outstanding(api):
    client, _, hooks = api
    first = client.post("/api/orders/return").json()
    assert client.post("/api/orders/return").status_code == 409

    # Once it has arrived, asking again is legitimate.
    hooks["status"]({"id": first["id"], "status": "completed", "attempts": 0,
                     "detail": "at the station, charging"})
    assert client.post("/api/orders/return").status_code == 201


def test_a_return_queues_behind_a_delivery(api):
    client, app_module, _ = api
    delivery = order(client, "SKU-1001").json()
    returning = client.post("/api/orders/return").json()
    assert app_module.db.next_pending_order()["id"] == delivery["id"]
    assert returning["id"] > delivery["id"]


def test_a_completed_return_touches_neither_stock_nor_history(api):
    client, app_module, hooks = api
    returning = client.post("/api/orders/return").json()
    hooks["status"]({"id": returning["id"], "status": "completed", "attempts": 0,
                     "detail": "at the station, charging"})

    assert app_module.db.list_history() == []
    assert len(client.get("/api/products").json()) == 6      # nothing was delivered
    assert client.get("/api/analytics").json()["returns_completed"] == 1


def test_a_return_reserves_energy_against_the_next_order(api):
    """It really does spend battery driving home, so the next order must be judged after it."""
    client, app_module, _ = api
    app_module.db.update_robot_state(battery_percent=60.0)
    app_module.ros.node.robot_xy = (-3.0, -2.2)      # parked at the far bay, 3 m from home
    client.post("/api/orders/return")

    preview = client.post("/api/preview",
                          json={"product_id": "SKU-1001", "delivery_id": "delivery_1"}).json()
    assert preview["estimate"]["reserved_orders"] == 1
    assert preview["estimate"]["battery_at_start_percent"] < 60.0


def test_a_return_from_the_station_costs_nothing(api):
    """The robot is already home, so the trip is zero metres - and must not reserve phantom Wh."""
    client, app_module, _ = api
    app_module.ros.node.robot_xy = None              # unknown position falls back to the station
    client.post("/api/orders/return")
    assert app_module.db.committed_load()["energy_wh"] == pytest.approx(0.0)


# -------------------------------------------------------------- emergency stop
def test_estop_is_one_shot_and_leaves_nothing_engaged(api):
    """It is an action with an end, not a mode. Nothing to clear, nothing to outlive it."""
    client, app_module, _ = api
    body = client.post("/api/estop").json()
    assert body == {"stopped": True, "robot_listening": True}
    assert app_module.ros.node.estops == ["stop"]
    # No state was written anywhere - so nothing survives the request, the session or a logout.
    assert "estop_engaged" not in app_module.db.get_robot_state()


def test_orders_still_work_after_a_stop(api):
    """The robot is left alone and idle, not locked out. The next order is an ordinary order."""
    client, _, _ = api
    client.post("/api/estop")
    assert order(client, "SKU-1001").json()["decision"] == "accepted"


def test_pressing_it_twice_is_harmless(api):
    client, app_module, _ = api
    client.post("/api/estop")
    client.post("/api/estop")
    assert app_module.ros.node.estops == ["stop", "stop"]


def test_the_stop_says_when_no_robot_was_listening(api):
    """Publishing to a topic with no subscribers succeeds silently - a stop button must not."""
    client, app_module, _ = api
    app_module.ros.node.subscription_count = 0
    assert client.post("/api/estop").json()["robot_listening"] is False


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
