"""Integration tests for the login gate and the two roles.

The point of a role is what it *cannot* do, so most of these assert a refusal. Templates are
loaded for real here - unlike the other integration suites, which run REST-only - because the
login flow is HTML forms, redirects and a cookie, and none of that is exercised by JSON.
"""
import importlib
import os
import pathlib
import re

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


def test_the_stylesheet_is_cache_busted(web):
    """A cached stylesheet is why a redesign can look like it never deployed.

    The version is the file's mtime, so editing the CSS invalidates the browser's copy with
    nothing to remember. Checked on a signed-out page too, since /login renders its own context.
    """
    client, _ = web
    login(client, "admin", "admin")
    for path in ("/admin", "/robot"):
        assert re.search(r'style\.css\?v=\d{6,}', client.get(path).text), path
    client.post("/logout")
    assert re.search(r'style\.css\?v=\d{6,}', client.get("/login").text)


def test_links_never_use_the_browser_default_colours(web):
    """Blue-then-purple is the browser's choice; every link state is pinned to the text colour."""
    css = pathlib.Path(WEB_DIR, "static", "style.css").read_text()
    rule = css[css.index("a, a:link"):css.index("}", css.index("a, a:link"))]
    for state in ("a:link", "a:visited", "a:hover", "a:active"):
        assert state in rule, state
    assert "color: inherit" in rule
    # And the dark theme makes that inherited colour white.
    assert "prefers-color-scheme: dark" in css
    assert "--fg:    #fff" in css


def test_the_login_page_itself_is_reachable_while_signed_out(web):
    client, _ = web
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    # No JavaScript, on this page as on every other (NFR3).
    assert "<script" not in response.text.lower()


def test_a_wrong_password_does_not_sign_you_in(web):
    client, _ = web
    response = login(client, "admin", "not-the-password")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert client.get("/robot").status_code == 303          # still shut out


def test_an_unknown_user_gets_the_same_message_as_a_wrong_password(web):
    """Otherwise the form tells an attacker which accounts exist."""
    client, _ = web
    unknown = login(client, "nobody", "x").headers["location"]
    wrong = login(client, "admin", "x").headers["location"]
    assert unknown == wrong


def test_signing_in_sets_an_httponly_cookie_and_opens_the_pages(web):
    client, _ = web
    response = login(client, "admin", "admin")
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "robofetch_session=" in cookie
    assert "httponly" in cookie.lower()
    assert client.get("/robot").status_code == 200


def test_logging_out_shuts_the_door_again(web):
    client, _ = web
    login(client, "admin", "admin")
    assert client.get("/robot").status_code == 200
    client.post("/logout")
    assert client.get("/robot").status_code == 303


def test_the_login_remembers_where_you_were_going(web):
    client, _ = web
    assert client.get("/orders").headers["location"] == "/login?next=/orders"


# ---------------------------------------------------------------- the roles
def test_a_controller_can_drive_the_robot(web):
    client, _ = web
    login(client, "controller", "controller")
    for path in GATED_PAGES:
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_a_controller_is_refused_the_admin_section(web, path):
    client, _ = web
    login(client, "controller", "controller")
    assert client.get(path).status_code == 403


def test_a_controller_cannot_change_the_catalogue_by_posting_directly(web):
    """The nav link is hidden from a controller, but hiding a link is not access control."""
    client, app_module = web
    login(client, "controller", "controller")
    response = client.post("/admin/products/delete", data={"product_id": "SKU-1001"})
    assert response.status_code == 403
    assert app_module.db.get_product("SKU-1001") is not None


def test_an_admin_reaches_everything(web):
    client, _ = web
    login(client, "admin", "admin")
    for path in GATED_PAGES + ADMIN_PAGES:
        assert client.get(path).status_code == 200, path


def test_the_admin_link_is_shown_only_to_admins(web):
    client, _ = web
    login(client, "controller", "controller")
    assert 'href="/admin"' not in client.get("/robot").text
    client.post("/logout")
    login(client, "admin", "admin")
    assert 'href="/admin"' in client.get("/robot").text


# ----------------------------------------------------------------- admin CRUD
def test_an_admin_can_create_update_and_delete_a_product(web):
    client, app_module = web
    login(client, "admin", "admin")
    fields = {"product_id": "SKU-NEW", "name": "Test widget", "category": "test",
              "weight_kg": "2.5", "stock": "1", "shelf_id": "shelf_1",
              "pick_point_id": "pick_1a", "model_name": "parcel_1"}

    assert client.post("/admin/products", data=fields).status_code == 303
    assert app_module.db.get_product("SKU-NEW")["name"] == "Test widget"

    client.post("/admin/products", data=dict(fields, name="Renamed", weight_kg="3.5"))
    updated = app_module.db.get_product("SKU-NEW")
    assert updated["name"] == "Renamed" and updated["weight_kg"] == 3.5

    client.post("/admin/products/delete", data={"product_id": "SKU-NEW"})
    assert app_module.db.get_product("SKU-NEW") is None


def test_an_admin_can_manage_delivery_points(web):
    client, app_module = web
    login(client, "admin", "admin")
    client.post("/admin/delivery-points",
                data={"delivery_id": "delivery_9", "name": "Test bay", "x": "1.5", "y": "-1.0"})
    assert app_module.db.get_delivery_point("delivery_9")["name"] == "Test bay"
    client.post("/admin/delivery-points/delete", data={"delivery_id": "delivery_9"})
    assert app_module.db.get_delivery_point("delivery_9") is None


def test_an_admin_can_add_a_user_and_change_a_password(web):
    client, app_module = web
    login(client, "admin", "admin")
    client.post("/admin/users/password",
                data={"username": "alice", "password": "s3cret", "role": "controller"})
    assert app_module.db.verify_user("alice", "s3cret")["role"] == "controller"
    # And the new account really can sign in.
    client.post("/logout")
    assert login(client, "alice", "s3cret").status_code == 303
    assert client.get("/robot").status_code == 200


def test_a_short_password_is_rejected(web):
    client, app_module = web
    login(client, "admin", "admin")
    response = client.post("/admin/users/password",
                           data={"username": "bob", "password": "xy", "role": "admin"})
    assert response.status_code == 400
    assert app_module.db.get_user("bob") is None


def test_an_admin_cannot_delete_their_own_account(web):
    """Deleting the account you are using locks you out of the only way back in."""
    client, app_module = web
    login(client, "admin", "admin")
    assert client.post("/admin/users/delete",
                       data={"username": "admin"}).status_code == 400
    assert app_module.db.get_user("admin") is not None


# ------------------------------------------------------------ estop in the UI
@pytest.mark.parametrize("path", GATED_PAGES + ADMIN_PAGES)
def test_the_stop_button_is_on_every_page_exactly_once(web, path):
    """One button, everywhere - you cannot know which page they will be looking at."""
    client, _ = web
    login(client, "admin", "admin")
    body = client.get(path).text
    assert body.count("EMERGENCY STOP") == 1, path
    assert 'class="estop-slot"' in body, path


def test_the_stop_leaves_nothing_behind_in_the_ui(web):
    """No banner, no clear button, nothing to switch off - and nothing survives a logout."""
    client, app_module = web
    login(client, "controller", "controller")
    client.post("/estop")
    assert app_module.ros.node.estops == ["stop"]

    body = client.get("/robot").text
    assert "ENGAGED" not in body
    assert "Clear" not in body
    assert body.count("EMERGENCY STOP") == 1

    client.post("/logout")
    login(client, "controller", "controller")
    assert "ENGAGED" not in client.get("/robot").text


def test_the_stop_never_queues_a_trip_home(web):
    """The robot is left where it stands - going home is a separate, deliberate decision."""
    client, app_module = web
    login(client, "controller", "controller")
    client.post("/estop")
    assert app_module.ros.node.returned == []
