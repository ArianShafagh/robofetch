"""Integration tests for the login gate and the two roles.

The gate is asserted against *every* page that requires a session, so a page added later
without protection fails here rather than shipping open. Templates are loaded for real -
unlike the REST-only integration suite - because the login flow is HTML forms, redirects
and a cookie, none of which JSON exercises.
"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "robofetch_web", "web")


class FakeNode:
    robot_xy = None

    def __init__(self):
        self.telemetry = {}
        self.submitted = []
        self.returned = []
        self.estops = []

    @property
    def order_pub(self):
        return self

    @property
    def estop_pub(self):
        return self

    def get_subscription_count(self):
        return 1

    def submit_order(self, order_id, product, pickup, dropoff):
        self.submitted.append(order_id)

    def submit_return(self, order_id, station):
        self.returned.append(order_id)

    def publish_estop(self, action):
        self.estops.append(action)
        return 1


@pytest.fixture
def web(tmp_path, monkeypatch):
    """The app with templates mounted, a throwaway database, no ROS and no AI."""
    monkeypatch.setenv("ROBOFETCH_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROBOFETCH_WEB", os.path.abspath(WEB_DIR))

    import robofetch_bridge.ros_link as ros_link

    class FakeRosThread:
        def __init__(self, on_status=None, on_telemetry=None):
            self.node = FakeNode()

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(ros_link, "RosThread", FakeRosThread)

    import robofetch_bridge.app as app_module
    importlib.reload(app_module)
    app_module.predictor = lambda features: None
    app_module.predictor.status = lambda: {"url": "fake", "reachable": False, "error": None}

    # follow_redirects=False so a redirect is observable rather than silently followed - the
    # redirect IS the behaviour under test for an anonymous visitor.
    with TestClient(app_module.app, follow_redirects=False) as client:
        yield client, app_module


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password,
                                       "next": "/"})


GATED_PAGES = ["/", "/orders", "/robot", "/return"]
ADMIN_PAGES = ["/admin", "/admin/products", "/admin/delivery-points", "/admin/db"]


# ----------------------------------------------------------------- the gate
@pytest.mark.parametrize("path", GATED_PAGES + ADMIN_PAGES)
def test_every_page_redirects_an_anonymous_visitor_to_the_login(web, path):
    client, _ = web
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# ------------------------------------------------------- the pages stay bare
def test_the_pages_carry_no_advice(web):
    """The UI shows data and controls only - guidance lives in the README, not on screen.

    Form labels like "Password" are fine; what must not appear is advice, tool names, other
    addresses to visit, or anything naming a credential.
    """
    client, _ = web
    login(client, "admin", "admin")
    advice = ("phpmyadmin", "sqlite-web", "localhost:8081", "default password",
              "see the readme", "pip install")
    for path in GATED_PAGES + ADMIN_PAGES + ["/login"]:
        body = client.get(path).text.lower()
        for phrase in advice:
            assert phrase not in body, f"{path} still mentions '{phrase}'"
