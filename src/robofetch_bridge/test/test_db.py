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


# -------------------------------------------------------------------- orders


def test_orders_are_served_oldest_first(db):
    first = db.create_order("SKU-1001", "delivery_1")
    db.create_order("SKU-2001", "delivery_2")
    assert db.next_pending_order()["id"] == first["id"]

    db.update_order(first["id"], status="completed")
    assert db.next_pending_order()["product_id"] == "SKU-2001"


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


# ------------------------------------------------------ users and sessions


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


# ------------------------------------------------------- database browser


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
