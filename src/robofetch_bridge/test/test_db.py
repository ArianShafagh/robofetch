"""Unit tests for the SQLite layer (proposal category A).

Plain sqlite3 in, plain dicts out - no ROS and no FastAPI, so these run in milliseconds.
"""
import os
import tempfile

import pytest

from robofetch_bridge.db import Database, ROLE_ADMIN, ROLE_CONTROLLER


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as folder:
        yield Database(os.path.join(folder, "test.db"))


# ------------------------------------------------------------------- seeding
def test_first_run_seeds_the_catalogue_and_the_layout(db):
    products = db.list_products()
    assert len(products) == 6
    assert {p["product_id"] for p in products} == {
        "SKU-1001", "SKU-1002", "SKU-2001", "SKU-2002", "SKU-3001", "SKU-3002"}
    assert len(db.list_shelves()) == 3
    assert len(db.list_pick_points()) == 6
    assert len(db.list_delivery_points()) == 2
    assert db.get_station()["station_id"] == "station_1"


def test_every_product_resolves_to_a_pick_point(db):
    """An order carries a product id, so the join to coordinates must never be missing."""
    for product in db.list_products():
        assert product["pick_x"] is not None
        assert product["pick_y"] is not None


def test_two_products_per_shelf_are_spaced_apart(db):
    """Parcels are not lifted when carried, so physical separation is what stops collisions."""
    by_shelf = {}
    for product in db.list_products():
        by_shelf.setdefault(product["shelf_id"], []).append(product)
    for shelf, items in by_shelf.items():
        assert len(items) == 2, shelf
        gap = abs(items[0]["pick_x"] - items[1]["pick_x"]) + \
              abs(items[0]["pick_y"] - items[1]["pick_y"])
        assert gap >= 0.8, f"{shelf} pick points are only {gap:.2f} m apart"


def test_delivery_points_are_far_apart(db):
    a, b = db.list_delivery_points()
    assert abs(a["x"] - b["x"]) > 4.0


def test_seeding_is_idempotent(db):
    Database(db.path)                       # open the same file again
    assert len(db.list_products()) == 6


# ------------------------------------------------------------------ products
def test_product_crud(db):
    assert db.get_product("SKU-9999") is None
    created = db.upsert_product("SKU-9999", "Test widget", "test", 2.5, 4,
                                "shelf_1", "pick_1a", "parcel_1")
    assert created["name"] == "Test widget"
    assert created["weight_kg"] == 2.5

    updated = db.upsert_product("SKU-9999", "Renamed", "test", 3.0, 1,
                                "shelf_1", "pick_1a", "parcel_1")
    assert updated["name"] == "Renamed"
    assert len([p for p in db.list_products() if p["product_id"] == "SKU-9999"]) == 1

    assert db.delete_product("SKU-9999") is True
    assert db.delete_product("SKU-9999") is False


def test_delivery_point_crud(db):
    db.upsert_delivery_point("delivery_9", "Test bay", 1.0, 2.0)
    assert db.get_delivery_point("delivery_9")["name"] == "Test bay"
    assert db.delete_delivery_point("delivery_9") is True
    assert db.get_delivery_point("delivery_9") is None


# -------------------------------------------------------------------- orders
def test_create_order_resolves_names_and_starts_pending(db):
    order = db.create_order("SKU-1001", "delivery_1")
    assert order["status"] == "pending"
    assert order["product_name"] == "Bearing set 6204-ZZ"
    assert order["weight_kg"] == 1.8
    assert order["delivery_name"] == "South-west bay"


def test_unknown_product_or_destination_is_rejected(db):
    with pytest.raises(ValueError, match="unknown product"):
        db.create_order("SKU-NOPE", "delivery_1")
    with pytest.raises(ValueError, match="unknown delivery point"):
        db.create_order("SKU-1001", "delivery_nope")


def test_the_admission_estimate_is_stored_with_the_order(db):
    """So the report can compare what was predicted against what happened."""
    order = db.create_order(
        "SKU-3001", "delivery_2",
        estimate={"distance_m": 9.1, "energy_wh": 6.2, "seconds": 58.0},
        decision="accepted", reason="fine", decided_by="model")
    assert order["estimated_distance_m"] == pytest.approx(9.1)
    assert order["estimated_energy_wh"] == pytest.approx(6.2)
    assert order["decision"] == "accepted"
    assert order["decided_by"] == "model"


def test_orders_are_served_oldest_first(db):
    first = db.create_order("SKU-1001", "delivery_1")
    db.create_order("SKU-2001", "delivery_2")
    assert db.next_pending_order()["id"] == first["id"]

    db.update_order(first["id"], status="completed")
    assert db.next_pending_order()["product_id"] == "SKU-2001"


def test_next_pending_order_is_none_when_the_queue_is_empty(db):
    assert db.next_pending_order() is None


def test_terminal_status_stamps_completed_at(db):
    order = db.create_order("SKU-1001", "delivery_1")
    assert order["completed_at"] is None
    assert db.update_order(order["id"], status="completed")["completed_at"] is not None


def test_cancel_only_while_pending(db):
    order = db.create_order("SKU-1001", "delivery_1")
    cancelled, error = db.cancel_order(order["id"])
    assert error is None and cancelled["status"] == "cancelled"

    again, error = db.cancel_order(order["id"])
    assert again is not None and "cannot cancel" in error

    missing, error = db.cancel_order(999)
    assert missing is None and error == "no such order"


# --------------------------------------------------------- reservation ledger
def test_committed_load_sums_unfinished_orders_only(db):
    """The ledger is derived from the orders table, so it cannot drift out of step with it."""
    estimate = {"distance_m": 8.0, "energy_wh": 4.0, "return_energy_wh": 1.0,
                "peak_temperature_c": 51.0, "seconds": 55.0}
    running = db.create_order("SKU-1001", "delivery_1", estimate=estimate)
    db.create_order("SKU-1002", "delivery_1", estimate=estimate)

    load = db.committed_load()
    assert load["orders"] == 2
    assert load["energy_wh"] == pytest.approx(10.0)      # 2 x (4.0 order + 1.0 return)
    assert load["peak_temperature_c"] == pytest.approx(51.0)

    # Finishing one gives its reservation back.
    db.update_order(running["id"], status="completed")
    assert db.committed_load()["orders"] == 1
    assert db.committed_load()["energy_wh"] == pytest.approx(5.0)


def test_committed_load_is_empty_when_nothing_is_running(db):
    load = db.committed_load()
    assert load == {"orders": 0, "energy_wh": 0.0, "peak_temperature_c": None}


def test_a_refused_order_reserves_nothing(db):
    """It was never going to run, so it must not hold energy against the next one."""
    order = db.create_order("SKU-1001", "delivery_1",
                            estimate={"energy_wh": 4.0, "return_energy_wh": 1.0})
    db.update_order(order["id"], status="refused")
    assert db.committed_load()["orders"] == 0


def test_an_order_stores_its_full_reservation(db):
    order = db.create_order(
        "SKU-3001", "delivery_2",
        estimate={"distance_m": 9.1, "energy_wh": 6.2, "return_energy_wh": 1.1,
                  "peak_temperature_c": 58.4, "seconds": 58.0})
    assert order["estimated_return_energy_wh"] == pytest.approx(1.1)
    assert order["estimated_peak_c"] == pytest.approx(58.4)


# --------------------------------------------------------------------- stock
def test_a_delivered_product_leaves_the_orderable_list(db):
    """Its parcel is at the bay, not on its shelf, so the robot cannot fetch it again."""
    assert len(db.list_products(available_only=True)) == 6

    db.consume_stock("SKU-1001")
    available = {p["product_id"] for p in db.list_products(available_only=True)}
    assert "SKU-1001" not in available
    assert len(available) == 5
    # It is gone from the choices, not from the catalogue - the operator still needs to see
    # where it went.
    assert len(db.list_products()) == 6
    assert db.get_product("SKU-1001")["stock"] == 0


def test_stock_never_goes_negative(db):
    for _ in range(3):
        db.consume_stock("SKU-1001")
    assert db.get_product("SKU-1001")["stock"] == 0


# -------------------------------------------------------- return-to-station
def test_a_return_is_an_order_with_no_product_or_destination(db):
    order = db.create_return_order(estimate={"distance_m": 3.0, "energy_wh": 1.1})
    assert order["kind"] == "return"
    assert order["status"] == "pending"
    assert order["product_id"] is None
    assert order["delivery_id"] is None
    # Always accepted: refusing a tired robot permission to go and charge is backwards.
    assert order["decision"] == "accepted"
    assert order["decided_by"] == "operator"


def test_a_return_queues_behind_deliveries(db):
    """FIFO across both kinds - the point of making it an order rather than a command."""
    first = db.create_order("SKU-1001", "delivery_1")
    db.create_return_order()
    assert db.next_pending_order()["id"] == first["id"]

    db.update_order(first["id"], status="completed")
    assert db.next_pending_order()["kind"] == "return"


def test_a_return_reserves_its_energy_like_any_other_order(db):
    db.create_return_order(estimate={"energy_wh": 1.4, "peak_temperature_c": 44.0})
    load = db.committed_load()
    assert load["orders"] == 1
    assert load["energy_wh"] == pytest.approx(1.4)


def test_only_one_return_can_be_outstanding(db):
    assert db.active_return() is None
    queued = db.create_return_order()
    assert db.active_return()["id"] == queued["id"]

    db.update_order(queued["id"], status="completed")
    assert db.active_return() is None


def test_analytics_count_deliveries_only(db):
    """A return is the operator moving the robot, not customer work it succeeded at."""
    delivered = db.create_order("SKU-1001", "delivery_1")
    db.update_order(delivered["id"], status="completed")
    returned = db.create_return_order()
    db.update_order(returned["id"], status="completed")

    stats = db.analytics()
    assert stats["total_orders"] == 1              # the return is not a delivery
    assert stats["completed"] == 1
    assert stats["success_rate"] == pytest.approx(1.0)
    assert stats["returns_requested"] == 1
    assert stats["returns_completed"] == 1


def test_a_failed_return_does_not_dent_the_delivery_success_rate(db):
    delivered = db.create_order("SKU-1001", "delivery_1")
    db.update_order(delivered["id"], status="completed")
    stranded = db.create_return_order()
    db.update_order(stranded["id"], status="failed")

    stats = db.analytics()
    assert stats["failed"] == 0
    assert stats["success_rate"] == pytest.approx(1.0)
    assert stats["returns_completed"] == 0


# ------------------------------------------------------ users and sessions
def test_the_two_roles_are_seeded(db):
    users = {u["username"]: u["role"] for u in db.list_users()}
    assert users == {"admin": ROLE_ADMIN, "controller": ROLE_CONTROLLER}


def test_passwords_are_salted_and_never_returned(db):
    """Two accounts with the same password must not share a hash, or one crack breaks both."""
    db.create_user("alice", "samepassword", ROLE_ADMIN)
    db.create_user("bob", "samepassword", ROLE_CONTROLLER)
    with db._connect() as conn:
        rows = {r["username"]: dict(r) for r in conn.execute("SELECT * FROM users")}
    assert rows["alice"]["salt"] != rows["bob"]["salt"]
    assert rows["alice"]["password_hash"] != rows["bob"]["password_hash"]
    assert "samepassword" not in str(rows)
    # The public accessors never expose the material at all.
    assert set(db.get_user("alice")) == {"username", "role", "created_at"}


def test_verify_user_accepts_the_right_password_only(db):
    db.create_user("alice", "correct-horse", ROLE_ADMIN)
    assert db.verify_user("alice", "correct-horse") == {"username": "alice",
                                                       "role": ROLE_ADMIN}
    assert db.verify_user("alice", "Correct-horse") is None
    assert db.verify_user("alice", "") is None
    assert db.verify_user("nobody", "correct-horse") is None


def test_changing_a_password_invalidates_the_old_one(db):
    db.create_user("alice", "first", ROLE_ADMIN)
    db.create_user("alice", "second", ROLE_ADMIN)
    assert db.verify_user("alice", "first") is None
    assert db.verify_user("alice", "second") is not None
    assert len([u for u in db.list_users() if u["username"] == "alice"]) == 1


def test_an_unknown_role_is_rejected(db):
    with pytest.raises(ValueError, match="unknown role"):
        db.create_user("alice", "pw", "superuser")


def test_sessions_are_created_looked_up_and_deleted(db):
    token = db.create_session("admin", ROLE_ADMIN)
    assert db.get_session(token)["role"] == ROLE_ADMIN
    assert db.get_session("not-a-token") is None
    assert db.get_session(None) is None
    db.delete_session(token)
    assert db.get_session(token) is None


def test_deleting_a_user_logs_them_out(db):
    db.create_user("alice", "pw", ROLE_ADMIN)
    token = db.create_session("alice", ROLE_ADMIN)
    db.delete_user("alice")
    assert db.get_session(token) is None


def test_default_passwords_are_reported_until_changed(db):
    assert set(db.uses_default_passwords()) == {"admin", "controller"}
    db.create_user("admin", "something-else", ROLE_ADMIN)
    assert db.uses_default_passwords() == ["controller"]


# ---------------------------------------------------------- emergency stop
def test_the_stop_stores_nothing(db):
    """It is a one-shot action, not a mode: no row, no column, nothing to outlive a session."""
    assert "estop" not in " ".join(db.get_robot_state())
    assert not hasattr(db, "set_estop")


# ------------------------------------------------------- database browser
def test_the_browser_lists_every_table_with_counts(db):
    names = {t["name"] for t in db.table_summary()}
    assert {"products", "orders", "users", "sessions", "robot_state"} <= names
    products = next(t for t in db.table_summary() if t["name"] == "products")
    assert products["rows"] == 6


def test_the_browser_refuses_a_table_that_does_not_exist(db):
    """Table names cannot be parameterised in SQL, so this check is the injection defence."""
    with pytest.raises(ValueError, match="no such table"):
        db.table_page("products; DROP TABLE products")
    with pytest.raises(ValueError, match="no such table"):
        db.table_count("sqlite_master")
    assert len(db.list_products()) == 6          # still there


def test_the_browser_redacts_secrets(db):
    db.create_user("alice", "pw", ROLE_ADMIN)
    db.create_session("alice", ROLE_ADMIN)
    for table, column in (("users", "password_hash"), ("users", "salt"),
                          ("sessions", "token")):
        page = db.table_page(table)
        assert all(row[column] == "••• redacted" for row in page["rows"]), (table, column)


def test_the_browser_pages_through_rows(db):
    for battery in range(10):
        db.record_telemetry({"battery_percent": float(battery), "state": "working"})
    first = db.table_page("robot_telemetry", limit=4, offset=0)
    second = db.table_page("robot_telemetry", limit=4, offset=4)
    assert len(first["rows"]) == 4 and len(second["rows"]) == 4
    assert first["rows"][0]["id"] != second["rows"][0]["id"]
    assert db.table_count("robot_telemetry") == 10


# ------------------------------------------------------------- session reset
def test_reset_session_clears_the_history_and_restocks_the_shelves(db):
    order = db.create_order("SKU-1001", "delivery_1", estimate={"energy_wh": 4.0})
    db.update_order(order["id"], status="completed")
    db.record_history(order["id"], 61.0, 9.2, 6.4, 1.8, "completed")
    db.record_telemetry({"battery_percent": 40.0, "state": "working"})
    db.update_robot_state(battery_percent=31.0, temperature_c=58.0, state="working")
    db.consume_stock("SKU-1001")

    token = db.create_session("admin", ROLE_ADMIN)

    db.reset_session()

    # Accounts outlive a run; sessions do not.
    assert db.get_session(token) is None
    assert len(db.list_users()) == 2
    assert db.list_orders() == []
    assert db.list_history() == []
    assert db.list_telemetry() == []
    assert db.committed_load()["orders"] == 0
    # Gazebo restores the same world every launch, so the catalogue must match it.
    assert all(p["stock"] == 1 for p in db.list_products())
    current = db.get_robot_state()
    assert current["battery_percent"] == pytest.approx(100.0)
    assert current["temperature_c"] == pytest.approx(22.0)
    assert current["state"] == "charging"


def test_reset_session_leaves_the_warehouse_layout_alone(db):
    db.reset_session()
    assert len(db.list_shelves()) == 3
    assert len(db.list_pick_points()) == 6
    assert len(db.list_delivery_points()) == 2
    assert db.get_station()["station_id"] == "station_1"


def test_orders_still_work_after_a_reset(db):
    """The tables are DROPped, so anything holding a stale handle would break here."""
    db.reset_session()
    order = db.create_order("SKU-1001", "delivery_1", estimate={"energy_wh": 4.0})
    assert order["id"] == 1                      # AUTOINCREMENT starts over
    assert db.next_pending_order()["product_id"] == "SKU-1001"


# ------------------------------------------------------- telemetry & history
def test_robot_state_updates_only_the_fields_supplied(db):
    db.update_robot_state(battery_percent=64.0, temperature_c=41.0)
    db.update_robot_state(state="working")
    current = db.get_robot_state()
    assert current["battery_percent"] == pytest.approx(64.0)
    assert current["temperature_c"] == pytest.approx(41.0)
    assert current["state"] == "working"


def test_telemetry_is_appended_newest_first(db):
    for battery in (90.0, 80.0, 70.0):
        db.record_telemetry({"battery_percent": battery, "state": "working"})
    samples = db.list_telemetry(limit=10)
    assert len(samples) == 3
    assert samples[0]["battery_percent"] == pytest.approx(70.0)


def test_history_records_the_outcome(db):
    order = db.create_order("SKU-1001", "delivery_1")
    db.record_history(order["id"], 61.0, 9.2, 6.4, 1.8, "completed")
    history = db.list_history()
    assert len(history) == 1 and history[0]["outcome"] == "completed"
    assert history[0]["energy_wh"] == pytest.approx(6.4)


# ------------------------------------------------------------------ analytics
def test_analytics_counts_every_terminal_state(db):
    completed = db.create_order("SKU-1001", "delivery_1")
    failed = db.create_order("SKU-1002", "delivery_1")
    refused = db.create_order("SKU-2001", "delivery_2")
    db.create_order("SKU-2002", "delivery_2")               # left pending

    db.update_order(completed["id"], status="completed")
    db.update_order(failed["id"], status="failed")
    db.update_order(refused["id"], status="refused")

    stats = db.analytics()
    assert stats["total_orders"] == 4
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["refused"] == 1
    assert stats["pending"] == 1
    # Rate is over FINISHED orders only: queued and refused work must not drag it down.
    assert stats["success_rate"] == pytest.approx(0.5)


def test_analytics_on_an_empty_database(db):
    stats = db.analytics()
    assert stats["total_orders"] == 0
    assert stats["success_rate"] is None
    assert stats["average_delivery_seconds"] is None
