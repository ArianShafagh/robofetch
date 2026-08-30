"""Integration tests for the web tier (proposal §12, Testing phase).

The unit suites cover pure logic - the condition model, the admission policy, the database.
None of them check that the pieces fit together, which is where the expensive bugs have lived:
an order that reaches SQLite but never reaches the robot looks exactly like one that worked.

This covers the primary SEAM, end to end:

    HTTP request -> admission -> SQLite row -> JSON published on /orders/new

ROS and the AI service are both faked, so the file runs in about a second with no simulator
and no network. Physical behaviour is covered by scripts/acceptance.py.
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
