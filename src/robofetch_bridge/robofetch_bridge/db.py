"""SQLite persistence for RoboFetch (proposal 2.1 and 5.2).

Four tables, exactly as the design section specifies:

    Orders          (id, item, point_a, point_b, status, retries, created_at, completed_at)
    Locations       (name, x, y)                     -- the waypoint registry
    DeliveryHistory (order_id, duration, distance, outcome)
    RobotState      (position, battery, last_update)

Everything here is plain sqlite3 and ordinary CRUD - no ROS, no FastAPI - so it can be
unit-tested on its own and swapped out later.

Waypoints are stored by NAME. A client orders "bring item_1 from shelf_1 to delivery_1"
and the names are resolved to coordinates here, which is why the Locations table exists
rather than clients sending raw numbers.
"""
import sqlite3
import time
from contextlib import contextmanager

# The warehouse's known waypoints, inserted on first run so the system is usable
# immediately. Coordinates match warehouse.sdf (map frame == Gazebo world frame).
DEFAULT_LOCATIONS = [
    ("shelf_1", -2.5, 0.95),      # in front of the north-west shelf
    ("shelf_2", 1.5, 0.95),       # in front of the north-east shelf
    ("shelf_3", 2.75, -1.0),      # in front of the east shelf
    ("delivery_1", -3.1, -2.2),   # three spots inside the delivery station, spread out
    ("delivery_2", -2.1, -2.2),   # so parcels do not pile up on one point
    ("delivery_3", -2.6, -1.5),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    name TEXT PRIMARY KEY,
    x    REAL NOT NULL,
    y    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item         TEXT NOT NULL,
    point_a      TEXT NOT NULL,          -- location name (pickup)
    point_b      TEXT NOT NULL,          -- location name (drop-off)
    status       TEXT NOT NULL DEFAULT 'pending',
    retries      INTEGER NOT NULL DEFAULT 0,
    detail       TEXT DEFAULT '',
    created_at   REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS delivery_history (
    order_id INTEGER PRIMARY KEY,
    duration REAL,
    distance REAL,
    outcome  TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS robot_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    x           REAL,
    y           REAL,
    battery     REAL DEFAULT 100.0,
    status      TEXT DEFAULT 'idle',
    last_update REAL
);
"""


class Database:
    """Thin wrapper around a SQLite file.

    A new connection is opened per operation (`check_same_thread=False` plus short-lived
    connections) because FastAPI request handlers and the ROS executor thread both write
    here.
    """

    def __init__(self, path):
        self.path = str(path)
        self._setup()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _setup(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            existing = conn.execute("SELECT COUNT(*) AS n FROM locations").fetchone()["n"]
            if existing == 0:
                conn.executemany(
                    "INSERT INTO locations (name, x, y) VALUES (?, ?, ?)",
                    DEFAULT_LOCATIONS)
            conn.execute(
                "INSERT OR IGNORE INTO robot_state (id, x, y, battery, status, last_update)"
                " VALUES (1, 0.0, 0.0, 100.0, 'idle', ?)", (time.time(),))

    # ------------------------------------------------------------------ locations
    def list_locations(self):
        with self._connect() as conn:
            return [dict(r) for r in
                    conn.execute("SELECT name, x, y FROM locations ORDER BY name")]

    def get_location(self, name):
        with self._connect() as conn:
            row = conn.execute("SELECT name, x, y FROM locations WHERE name = ?",
                               (name,)).fetchone()
            return dict(row) if row else None

    def upsert_location(self, name, x, y):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO locations (name, x, y) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET x = excluded.x, y = excluded.y",
                (name, float(x), float(y)))
        return self.get_location(name)

    def delete_location(self, name):
        with self._connect() as conn:
            return conn.execute("DELETE FROM locations WHERE name = ?",
                                (name,)).rowcount > 0

    # --------------------------------------------------------------------- orders
    def create_order(self, item, point_a, point_b):
        """Insert a PENDING order. Both waypoints must already exist."""
        if self.get_location(point_a) is None:
            raise ValueError(f"unknown pickup location '{point_a}'")
        if self.get_location(point_b) is None:
            raise ValueError(f"unknown drop-off location '{point_b}'")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO orders (item, point_a, point_b, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (item, point_a, point_b, time.time()))
            new_id = cur.lastrowid
        # Read back only AFTER the transaction has committed - get_order opens its own
        # connection and would otherwise not see the new row.
        return self.get_order(new_id)

    def get_order(self, order_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return dict(row) if row else None

    def list_orders(self, status=None):
        query = "SELECT * FROM orders"
        params = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query, params)]

    def update_order(self, order_id, status=None, retries=None, detail=None):
        """Record a state change coming back from the robot."""
        sets, params = [], []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status in ("completed", "failed", "cancelled"):
                sets.append("completed_at = ?")
                params.append(time.time())
        if retries is not None:
            sets.append("retries = ?")
            params.append(int(retries))
        if detail is not None:
            sets.append("detail = ?")
            params.append(detail)
        if not sets:
            return self.get_order(order_id)
        params.append(order_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_order(order_id)

    def cancel_order(self, order_id):
        """UC: a client may cancel an order only while it is still PENDING."""
        order = self.get_order(order_id)
        if order is None:
            return None, "no such order"
        if order["status"] != "pending":
            return order, f"cannot cancel an order that is already '{order['status']}'"
        return self.update_order(order_id, status="cancelled",
                                 detail="cancelled by the client"), None

    # ------------------------------------------------------------------- history
    def record_history(self, order_id, duration, distance, outcome):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO delivery_history (order_id, duration, distance, outcome) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET "
                "duration = excluded.duration, distance = excluded.distance, "
                "outcome = excluded.outcome",
                (order_id, duration, distance, outcome))

    def list_history(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM delivery_history ORDER BY order_id")]

    # ---------------------------------------------------------------- robot state
    def update_robot_state(self, x=None, y=None, battery=None, status=None):
        sets, params = ["last_update = ?"], [time.time()]
        for column, value in (("x", x), ("y", y), ("battery", battery),
                              ("status", status)):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        with self._connect() as conn:
            conn.execute(f"UPDATE robot_state SET {', '.join(sets)} WHERE id = 1", params)

    def get_robot_state(self):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM robot_state WHERE id = 1").fetchone()
            return dict(row) if row else {}

    # ------------------------------------------------------------------ analytics
    def analytics(self):
        """Aggregate statistics (proposal 2.1: simple SQL, no custom algorithm)."""
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(status = 'completed') AS completed,"
                " SUM(status = 'failed')    AS failed,"
                " SUM(status = 'pending')   AS pending,"
                " SUM(status = 'cancelled') AS cancelled"
                " FROM orders").fetchone()
            avg_row = conn.execute(
                "SELECT AVG(completed_at - created_at) AS avg_seconds FROM orders "
                "WHERE status = 'completed' AND completed_at IS NOT NULL").fetchone()

        total = totals["total"] or 0
        completed = totals["completed"] or 0
        failed = totals["failed"] or 0
        finished = completed + failed
        return {
            "total_orders": total,
            "completed": completed,
            "failed": failed,
            "pending": totals["pending"] or 0,
            "cancelled": totals["cancelled"] or 0,
            # Success rate is over FINISHED orders; counting pending ones as failures
            # would make the figure drift down simply because work is queued.
            "success_rate": round(completed / finished, 3) if finished else None,
            "average_delivery_seconds": (round(avg_row["avg_seconds"], 1)
                                         if avg_row["avg_seconds"] else None),
        }
