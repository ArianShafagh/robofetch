"""Unit tests for the SQLite layer (proposal category A).

Plain sqlite3 in, plain dicts out - no ROS and no FastAPI, so these run in milliseconds.
"""
import os
import tempfile

import pytest

from robofetch_bridge.db import Database


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
