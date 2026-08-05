"""Unit tests for the SQLite persistence layer (proposal 2.1, 5.2, 5.4).

Covers the CRUD operations and the analytics aggregates, including the rule that an
order can only be cancelled while it is still pending.

Run with:  pytest src/robofetch_bridge/test/test_db.py -v
"""
import pytest

from robofetch_bridge.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


# ------------------------------------------------------------------- locations
def test_default_locations_are_seeded(db):
    names = [loc["name"] for loc in db.list_locations()]
    assert "shelf_1" in names and "delivery_1" in names


def test_add_and_read_back_a_location(db):
    db.upsert_location("bay_7", 1.25, -0.5)
    assert db.get_location("bay_7") == {"name": "bay_7", "x": 1.25, "y": -0.5}


def test_upsert_updates_an_existing_location(db):
    db.upsert_location("bay_7", 1.0, 1.0)
    db.upsert_location("bay_7", 2.0, 3.0)
    assert db.get_location("bay_7")["x"] == 2.0
    assert len([l for l in db.list_locations() if l["name"] == "bay_7"]) == 1


def test_delete_location(db):
    db.upsert_location("temp", 0.0, 0.0)
    assert db.delete_location("temp") is True
    assert db.get_location("temp") is None
    assert db.delete_location("temp") is False


# ---------------------------------------------------------------------- orders
def test_create_order_starts_pending(db):
    order = db.create_order("item_1", "shelf_1", "delivery_1")
    assert order["status"] == "pending"
    assert order["item"] == "item_1"
    assert order["retries"] == 0
    assert order["completed_at"] is None


def test_order_with_unknown_location_is_rejected(db):
    with pytest.raises(ValueError, match="unknown pickup"):
        db.create_order("item_1", "nowhere", "delivery_1")
    with pytest.raises(ValueError, match="unknown drop-off"):
        db.create_order("item_1", "shelf_1", "nowhere")


def test_list_orders_can_filter_by_status(db):
    a = db.create_order("item_1", "shelf_1", "delivery_1")
    db.create_order("item_2", "shelf_2", "delivery_2")
    db.update_order(a["id"], status="completed")
    assert len(db.list_orders()) == 2
    assert len(db.list_orders(status="pending")) == 1
    assert len(db.list_orders(status="completed")) == 1


def test_completing_an_order_stamps_completed_at(db):
    order = db.create_order("item_1", "shelf_1", "delivery_1")
    updated = db.update_order(order["id"], status="completed", detail="delivered")
    assert updated["completed_at"] is not None
    assert updated["detail"] == "delivered"


def test_retries_are_recorded(db):
    order = db.create_order("item_1", "shelf_1", "delivery_1")
    updated = db.update_order(order["id"], retries=3, status="failed")
    assert updated["retries"] == 3


# -------------------------------------------------------------------- cancelling
def test_pending_order_can_be_cancelled(db):
    order = db.create_order("item_1", "shelf_1", "delivery_1")
    cancelled, error = db.cancel_order(order["id"])
    assert error is None
    assert cancelled["status"] == "cancelled"


def test_order_in_progress_cannot_be_cancelled(db):
    order = db.create_order("item_1", "shelf_1", "delivery_1")
    db.update_order(order["id"], status="delivering")
    _, error = db.cancel_order(order["id"])
    assert error is not None and "cannot cancel" in error


def test_cancelling_a_missing_order_reports_an_error(db):
    order, error = db.cancel_order(999)
    assert order is None and error == "no such order"


# ------------------------------------------------------------------- robot state
def test_robot_state_round_trip(db):
    db.update_robot_state(x=1.5, y=-2.0, status="delivering")
    state = db.get_robot_state()
    assert state["x"] == 1.5 and state["y"] == -2.0
    assert state["status"] == "delivering"
    assert state["last_update"] is not None


# --------------------------------------------------------------------- analytics
def test_analytics_on_an_empty_database(db):
    stats = db.analytics()
    assert stats["total_orders"] == 0
    assert stats["success_rate"] is None
    assert stats["average_delivery_seconds"] is None


def test_analytics_counts_and_success_rate(db):
    ids = [db.create_order(f"item_{i}", "shelf_1", "delivery_1")["id"]
           for i in range(1, 5)]
    db.update_order(ids[0], status="completed")
    db.update_order(ids[1], status="completed")
    db.update_order(ids[2], status="failed")
    # ids[3] stays pending
    stats = db.analytics()
    assert stats["total_orders"] == 4
    assert stats["completed"] == 2 and stats["failed"] == 1 and stats["pending"] == 1
    # 2 of 3 FINISHED orders succeeded; the pending one must not drag the rate down.
    assert stats["success_rate"] == pytest.approx(0.667, abs=1e-3)


def test_history_round_trip(db):
    order = db.create_order("item_1", "shelf_1", "delivery_1")
    db.record_history(order["id"], duration=42.0, distance=7.5, outcome="completed")
    rows = db.list_history()
    assert len(rows) == 1 and rows[0]["duration"] == 42.0
