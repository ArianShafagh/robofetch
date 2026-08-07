"""SQLite persistence for RoboFetch v2 (proposal §3.1 category A, §6 data model).

Eight tables. The important change from v1 is that an order references a **product**, never a
coordinate:

    products         (product_id, name, category, weight_kg, stock, shelf, pick point, model)
    shelves          (shelf_id, centre)              -- warehouse layout ...
    pick_points      (pick_point_id, shelf, x, y)    -- ... one pick point per product
    delivery_points  (delivery_id, name, x, y)
    station          (station_id, x, y)              -- home / charging point
    orders           (product_id, delivery_id, status, attempts, estimate, decision, ...)
    delivery_history (order_id, duration, distance, energy, payload, outcome)
    robot_telemetry  (ts, run_id, order_id, battery, temperature, condition, ...)
    robot_state      (single row: the CURRENT robot state, for fast reads)

A client orders "SKU-3001 to delivery_2" and the coordinates are resolved here. That is what
makes the catalogue worth having: the warehouse layout can change without any client, or any
line of robot code, knowing about it.

Everything is plain sqlite3 - no ROS, no FastAPI - so it can be unit-tested on its own.
"""
import sqlite3
import time
from contextlib import contextmanager

# ---------------------------------------------------------------- seed layout
# Coordinates match warehouse.sdf (map frame == Gazebo world frame). All three shelves are
# flush against a wall; every waypoint keeps >=0.6 m clearance from walls and shelves
# (robot radius 0.22 m + 0.35 m costmap inflation).
DEFAULT_SHELVES = [
    ("shelf_1", -2.5, 2.25),      # north-west, flush to the north wall
    ("shelf_2", 1.5, 2.25),       # north-east, flush to the north wall
    ("shelf_3", 3.6, -1.0),       # east, flush to the east wall
]

# Two pick points per shelf, ~1 m apart, so the robot can reach one parcel without touching
# the other. Parcels are NOT lifted when carried (they stay below the lidar plane), so
# physical separation is what prevents them colliding.
DEFAULT_PICK_POINTS = [
    ("pick_1a", "shelf_1", -3.0, 0.95),
    ("pick_1b", "shelf_1", -2.0, 0.95),
    ("pick_2a", "shelf_2", 1.05, 0.95),
    ("pick_2b", "shelf_2", 1.95, 0.95),
    ("pick_3a", "shelf_3", 2.75, -1.5),
    ("pick_3b", "shelf_3", 2.75, -0.5),
]

# Opposite corners, 5.6 m apart. Parcels are below lidar height and therefore invisible to
# Nav2, so stacking deliveries on one point creates obstacles the robot cannot see.
DEFAULT_DELIVERY_POINTS = [
    ("delivery_1", "South-west bay", -3.0, -2.2),
    ("delivery_2", "South-east bay", 2.6, -2.2),
]

# Home and charging point, deliberately distinct from both delivery bays.
DEFAULT_STATION = ("station_1", 0.0, -2.2)

# The catalogue. `model_name` maps a business identifier to the physical simulator model,
# keeping the two namespaces separate the way a real system would.
DEFAULT_PRODUCTS = [
    ("SKU-1001", "Bearing set 6204-ZZ",      "mechanical",  1.8, "shelf_1", "pick_1a", "parcel_1"),
    ("SKU-1002", "Hydraulic seal kit",       "mechanical",  0.6, "shelf_1", "pick_1b", "parcel_2"),
    ("SKU-2001", "Servo drive unit",         "electronics", 3.2, "shelf_2", "pick_2a", "parcel_3"),
    ("SKU-2002", "Cable harness, 5 m",       "electronics", 1.1, "shelf_2", "pick_2b", "parcel_4"),
    ("SKU-3001", "Li-ion battery pack, 48 V", "power",      4.5, "shelf_3", "pick_3a", "parcel_5"),
    ("SKU-3002", "IR sensor module",         "sensors",     0.3, "shelf_3", "pick_3b", "parcel_6"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS shelves (
    shelf_id TEXT PRIMARY KEY,
    centre_x REAL NOT NULL,
    centre_y REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pick_points (
    pick_point_id TEXT PRIMARY KEY,
    shelf_id      TEXT NOT NULL,
    x             REAL NOT NULL,
    y             REAL NOT NULL,
    FOREIGN KEY (shelf_id) REFERENCES shelves(shelf_id)
);

CREATE TABLE IF NOT EXISTS delivery_points (
    delivery_id TEXT PRIMARY KEY,
    name        TEXT,
    x           REAL NOT NULL,
    y           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS station (
    station_id TEXT PRIMARY KEY,
    x          REAL NOT NULL,
    y          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT,
    weight_kg     REAL NOT NULL,
    stock         INTEGER NOT NULL DEFAULT 1,
    shelf_id      TEXT,
    pick_point_id TEXT,
    model_name    TEXT NOT NULL,       -- the Gazebo model this SKU physically is
    FOREIGN KEY (shelf_id)      REFERENCES shelves(shelf_id),
    FOREIGN KEY (pick_point_id) REFERENCES pick_points(pick_point_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id           TEXT NOT NULL,
    delivery_id          TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    attempts             INTEGER NOT NULL DEFAULT 0,
    detail               TEXT DEFAULT '',
    -- what admission control predicted before the order was accepted
    estimated_distance_m REAL,
    estimated_energy_wh  REAL,
    estimated_seconds    REAL,
    decision             TEXT,          -- accepted | refused
    decision_reason      TEXT,
    decided_by           TEXT,          -- model | fallback
    created_at           REAL NOT NULL,
    completed_at         REAL,
    FOREIGN KEY (product_id)  REFERENCES products(product_id),
    FOREIGN KEY (delivery_id) REFERENCES delivery_points(delivery_id)
);

CREATE TABLE IF NOT EXISTS delivery_history (
    order_id   INTEGER PRIMARY KEY,
    duration_s REAL,
    distance_m REAL,
    energy_wh  REAL,
    payload_kg REAL,
    outcome    TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS robot_telemetry (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    REAL NOT NULL,
    run_id                TEXT,
    order_id              INTEGER,
    battery_percent       REAL,
    temperature_c         REAL,
    condition_percent     REAL,
    payload_kg            REAL,
    motor_load            REAL,
    speed                 REAL,
    cumulative_distance_m REAL,
    state                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON robot_telemetry(ts);

CREATE TABLE IF NOT EXISTS robot_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    battery_percent       REAL DEFAULT 100.0,
    temperature_c         REAL DEFAULT 22.0,
    condition_percent     REAL DEFAULT 100.0,
    payload_kg            REAL DEFAULT 0.0,
    motor_load            REAL DEFAULT 0.0,
    cumulative_distance_m REAL DEFAULT 0.0,
    uptime_s              REAL DEFAULT 0.0,
    state                 TEXT DEFAULT 'charging',
    run_id                TEXT,
    last_update           REAL
);
"""


class Database:
    """Thin wrapper around a SQLite file.

    A new connection is opened per operation (short-lived, `check_same_thread=False`) because
    FastAPI request handlers and the ROS executor thread both write here.
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
            if conn.execute("SELECT COUNT(*) AS n FROM shelves").fetchone()["n"] == 0:
                conn.executemany("INSERT INTO shelves VALUES (?, ?, ?)", DEFAULT_SHELVES)
                conn.executemany("INSERT INTO pick_points VALUES (?, ?, ?, ?)",
                                 DEFAULT_PICK_POINTS)
                conn.executemany("INSERT INTO delivery_points VALUES (?, ?, ?, ?)",
                                 DEFAULT_DELIVERY_POINTS)
                conn.execute("INSERT INTO station VALUES (?, ?, ?)", DEFAULT_STATION)
            if conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"] == 0:
                conn.executemany(
                    "INSERT INTO products (product_id, name, category, weight_kg, shelf_id,"
                    " pick_point_id, model_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    DEFAULT_PRODUCTS)
            conn.execute(
                "INSERT OR IGNORE INTO robot_state (id, last_update) VALUES (1, ?)",
                (time.time(),))

    # ------------------------------------------------------------------- products
    def list_products(self):
        """Catalogue joined with its pick point, which is what an order actually needs."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT p.*, k.x AS pick_x, k.y AS pick_y FROM products p "
                "LEFT JOIN pick_points k ON k.pick_point_id = p.pick_point_id "
                "ORDER BY p.product_id")]

    def get_product(self, product_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.*, k.x AS pick_x, k.y AS pick_y FROM products p "
                "LEFT JOIN pick_points k ON k.pick_point_id = p.pick_point_id "
                "WHERE p.product_id = ?", (product_id,)).fetchone()
            return dict(row) if row else None

    def upsert_product(self, product_id, name, category, weight_kg, stock,
                       shelf_id, pick_point_id, model_name):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO products (product_id, name, category, weight_kg, stock,"
                " shelf_id, pick_point_id, model_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(product_id) DO UPDATE SET name = excluded.name,"
                " category = excluded.category, weight_kg = excluded.weight_kg,"
                " stock = excluded.stock, shelf_id = excluded.shelf_id,"
                " pick_point_id = excluded.pick_point_id, model_name = excluded.model_name",
                (product_id, name, category, float(weight_kg), int(stock),
                 shelf_id, pick_point_id, model_name))
        return self.get_product(product_id)

    def delete_product(self, product_id):
        with self._connect() as conn:
            return conn.execute("DELETE FROM products WHERE product_id = ?",
                                (product_id,)).rowcount > 0

    # --------------------------------------------------------------------- layout
    def list_delivery_points(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM delivery_points ORDER BY delivery_id")]

    def get_delivery_point(self, delivery_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM delivery_points WHERE delivery_id = ?",
                               (delivery_id,)).fetchone()
            return dict(row) if row else None

    def upsert_delivery_point(self, delivery_id, name, x, y):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO delivery_points VALUES (?, ?, ?, ?) "
                "ON CONFLICT(delivery_id) DO UPDATE SET name = excluded.name,"
                " x = excluded.x, y = excluded.y",
                (delivery_id, name, float(x), float(y)))
        return self.get_delivery_point(delivery_id)

    def delete_delivery_point(self, delivery_id):
        with self._connect() as conn:
            return conn.execute("DELETE FROM delivery_points WHERE delivery_id = ?",
                                (delivery_id,)).rowcount > 0

    def list_pick_points(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM pick_points ORDER BY pick_point_id")]

    def list_shelves(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM shelves ORDER BY shelf_id")]

    def get_station(self):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM station LIMIT 1").fetchone()
            return dict(row) if row else None

    # --------------------------------------------------------------------- orders
    def create_order(self, product_id, delivery_id, estimate=None, decision=None,
                     reason=None, decided_by=None):
        """Insert a PENDING order. Both the product and the destination must exist.

        The admission estimate is stored WITH the order so the report can compare what was
        predicted against what actually happened.
        """
        if self.get_product(product_id) is None:
            raise ValueError(f"unknown product '{product_id}'")
        if self.get_delivery_point(delivery_id) is None:
            raise ValueError(f"unknown delivery point '{delivery_id}'")
        estimate = estimate or {}
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO orders (product_id, delivery_id, status, estimated_distance_m,"
                " estimated_energy_wh, estimated_seconds, decision, decision_reason,"
                " decided_by, created_at) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (product_id, delivery_id, estimate.get("distance_m"),
                 estimate.get("energy_wh"), estimate.get("seconds"),
                 decision, reason, decided_by, time.time()))
            new_id = cur.lastrowid
        # Read back only AFTER the transaction commits - get_order opens its own connection
        # and would otherwise not see the new row.
        return self.get_order(new_id)

    def get_order(self, order_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT o.*, p.name AS product_name, p.weight_kg, d.name AS delivery_name "
                "FROM orders o "
                "LEFT JOIN products p ON p.product_id = o.product_id "
                "LEFT JOIN delivery_points d ON d.delivery_id = o.delivery_id "
                "WHERE o.id = ?", (order_id,)).fetchone()
            return dict(row) if row else None

    def list_orders(self, status=None, limit=None):
        query = ("SELECT o.*, p.name AS product_name, p.weight_kg,"
                 " d.name AS delivery_name FROM orders o "
                 "LEFT JOIN products p ON p.product_id = o.product_id "
                 "LEFT JOIN delivery_points d ON d.delivery_id = o.delivery_id")
        params = []
        if status:
            query += " WHERE o.status = ?"
            params.append(status)
        query += " ORDER BY o.id"
        if limit:
            query += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query, params)]

    def next_pending_order(self):
        """FIFO: the oldest order still waiting. Customers are served in the order they asked."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE status = 'pending' "
                "ORDER BY created_at, id LIMIT 1").fetchone()
            return dict(row) if row else None

    def update_order(self, order_id, status=None, attempts=None, detail=None):
        sets, params = [], []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status in ("completed", "failed", "cancelled", "refused"):
                sets.append("completed_at = ?")
                params.append(time.time())
        if attempts is not None:
            sets.append("attempts = ?")
            params.append(int(attempts))
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
        """A client may cancel an order only while it is still PENDING."""
        order = self.get_order(order_id)
        if order is None:
            return None, "no such order"
        if order["status"] != "pending":
            return order, f"cannot cancel an order that is already '{order['status']}'"
        return self.update_order(order_id, status="cancelled",
                                 detail="cancelled by the client"), None

    # -------------------------------------------------------------------- history
    def record_history(self, order_id, duration_s, distance_m, energy_wh, payload_kg,
                       outcome):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO delivery_history VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET duration_s = excluded.duration_s,"
                " distance_m = excluded.distance_m, energy_wh = excluded.energy_wh,"
                " payload_kg = excluded.payload_kg, outcome = excluded.outcome",
                (order_id, duration_s, distance_m, energy_wh, payload_kg, outcome))

    def list_history(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM delivery_history ORDER BY order_id")]

    # ---------------------------------------------------------------- robot state
    def update_robot_state(self, **fields):
        """Overwrite the single current-state row with whatever fields were supplied."""
        allowed = ("battery_percent", "temperature_c", "condition_percent", "payload_kg",
                   "motor_load", "cumulative_distance_m", "uptime_s", "state", "run_id")
        sets, params = ["last_update = ?"], [time.time()]
        for column in allowed:
            if fields.get(column) is not None:
                sets.append(f"{column} = ?")
                params.append(fields[column])
        with self._connect() as conn:
            conn.execute(f"UPDATE robot_state SET {', '.join(sets)} WHERE id = 1", params)

    def get_robot_state(self):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM robot_state WHERE id = 1").fetchone()
            return dict(row) if row else {}

    def record_telemetry(self, sample):
        """Append one telemetry sample. This is the time-series the ML training set is built from."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO robot_telemetry (ts, run_id, order_id, battery_percent,"
                " temperature_c, condition_percent, payload_kg, motor_load, speed,"
                " cumulative_distance_m, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sample.get("ts", time.time()), sample.get("run_id"), sample.get("order_id"),
                 sample.get("battery_percent"), sample.get("temperature_c"),
                 sample.get("condition_percent"), sample.get("payload_kg"),
                 sample.get("motor_load"), sample.get("speed"),
                 sample.get("cumulative_distance_m"), sample.get("state")))

    def list_telemetry(self, limit=500):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM robot_telemetry ORDER BY ts DESC LIMIT ?", (int(limit),))]

    # ------------------------------------------------------------------ analytics
    def analytics(self):
        """Aggregate statistics (proposal §3.1 A8: simple SQL, no custom algorithm)."""
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(status = 'completed') AS completed,"
                " SUM(status = 'failed')    AS failed,"
                " SUM(status = 'pending')   AS pending,"
                " SUM(status = 'cancelled') AS cancelled,"
                " SUM(status = 'refused')   AS refused"
                " FROM orders").fetchone()
            avg_row = conn.execute(
                "SELECT AVG(completed_at - created_at) AS avg_seconds FROM orders "
                "WHERE status = 'completed' AND completed_at IS NOT NULL").fetchone()
            energy = conn.execute(
                "SELECT AVG(energy_wh) AS avg_energy, SUM(energy_wh) AS total_energy,"
                " AVG(distance_m) AS avg_distance FROM delivery_history "
                "WHERE outcome = 'completed'").fetchone()

        total = totals["total"] or 0
        completed = totals["completed"] or 0
        failed = totals["failed"] or 0
        finished = completed + failed
        avg_energy = energy["avg_energy"]
        avg_distance = energy["avg_distance"]
        return {
            "total_orders": total,
            "completed": completed,
            "failed": failed,
            "pending": totals["pending"] or 0,
            "cancelled": totals["cancelled"] or 0,
            "refused": totals["refused"] or 0,
            # Success rate is over FINISHED orders; counting pending ones as failures would
            # make the figure drift down simply because work is queued.
            "success_rate": round(completed / finished, 3) if finished else None,
            "average_delivery_seconds": (round(avg_row["avg_seconds"], 1)
                                         if avg_row["avg_seconds"] else None),
            "average_energy_wh": round(avg_energy, 2) if avg_energy else None,
            "average_distance_m": round(avg_distance, 2) if avg_distance else None,
            "total_energy_wh": round(energy["total_energy"], 2) if energy["total_energy"] else None,
        }
