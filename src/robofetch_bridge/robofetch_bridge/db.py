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
import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

# ------------------------------------------------------------------------- roles
ROLE_ADMIN = "admin"
ROLE_CONTROLLER = "controller"
ROLES = (ROLE_ADMIN, ROLE_CONTROLLER)

# Seeded on first run so the system is usable immediately. Override with the environment
# before first launch; after that, change them from the admin page. These defaults are
# deliberately obvious rather than secret - see the warning the admin page shows while they
# are still in use.
DEFAULT_USERS = (
    (ROLE_ADMIN, "ROBOFETCH_ADMIN_PASSWORD", "admin"),
    (ROLE_CONTROLLER, "ROBOFETCH_CONTROLLER_PASSWORD", "controller"),
)
PBKDF2_ROUNDS = 200_000

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

# The two things the robot can be asked to do. A "return" is an order in its own right so that
# it goes through the same queue, the same status reporting and the same reservation ledger as a
# delivery - the operator can see it waiting its turn on /orders like anything else.
KIND_DELIVERY = "delivery"
KIND_RETURN = "return"

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
    -- 'delivery' (fetch a product to a bay) or 'return' (go home to the station). A return is
    -- a real order rather than a side-channel command precisely so it QUEUES: interrupting a
    -- delivery to drive home would leave a parcel half-carried.
    kind                 TEXT NOT NULL DEFAULT 'delivery',
    -- Both NULL for a return: there is no parcel and the destination is the station, which is
    -- not a delivery point. Everything reading these already uses LEFT JOINs.
    product_id           TEXT,
    delivery_id          TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    attempts             INTEGER NOT NULL DEFAULT 0,
    detail               TEXT DEFAULT '',
    -- what admission control predicted before the order was accepted. These are not only a
    -- record for the report: while the order is unfinished they are its RESERVATION, the
    -- energy and heat the next admission decision must assume are already spoken for.
    estimated_distance_m REAL,
    estimated_energy_wh  REAL,
    estimated_return_energy_wh REAL,    -- the leg home, costed but never shown to the customer
    estimated_peak_c     REAL,          -- hottest the motors are predicted to get
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

-- Two roles, because the two jobs need different things. A robot controller places orders and
-- stops the robot; an administrator also owns the catalogue and the database itself. Passwords
-- are salted PBKDF2 - not because this simulator is a security target, but because a coursework
-- project that stored them in plain text would be teaching the wrong lesson.
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL,          -- admin | controller
    created_at    REAL NOT NULL
);

-- Server-side sessions rather than signed cookies: the cookie holds nothing but a random
-- token, so there is no secret key to leak or rotate, and an administrator can see exactly who
-- is logged in by looking at this table. Cleared on every launch along with the run history.
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL,
    created_at REAL NOT NULL
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

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` silently leaves an
    # existing table alone, so a database written by an older build would keep the old shape
    # and every query naming a new column would fail. reset_session() drops and recreates the
    # order tables anyway, but tools and scripts open the file WITHOUT resetting it.
    # Note this can only ADD columns. `kind` therefore carries a NOT NULL default so it lands
    # on an old table cleanly; the matching relaxation of product_id/delivery_id to nullable
    # cannot be done by ALTER, and does not need to be - reset_session() rebuilds the table on
    # every launch, and only a launched system ever writes a return.
    MIGRATIONS = {
        "orders": [("estimated_return_energy_wh", "REAL"), ("estimated_peak_c", "REAL"),
                   ("kind", "TEXT NOT NULL DEFAULT 'delivery'")],
    }

    def _setup(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            for table, columns in self.MIGRATIONS.items():
                existing = {row["name"] for row in
                            conn.execute(f"PRAGMA table_info({table})")}
                for column, kind in columns:
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
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
            seed_users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
        # Outside the transaction above: create_user opens its own connection.
        if seed_users:
            for role, env_var, default in DEFAULT_USERS:
                self.create_user(role, os.environ.get(env_var, default), role)

    # ----------------------------------------------------------------------- users
    @staticmethod
    def _hash_password(password, salt):
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()

    def create_user(self, username, password, role):
        """Create or replace a user. Role must be one of ROLES."""
        if role not in ROLES:
            raise ValueError(f"unknown role '{role}'")
        salt = secrets.token_hex(16)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, created_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET"
                " password_hash = excluded.password_hash, salt = excluded.salt,"
                " role = excluded.role",
                (username, self._hash_password(password, salt), salt, role, time.time()))
        return self.get_user(username)

    def get_user(self, username):
        with self._connect() as conn:
            row = conn.execute("SELECT username, role, created_at FROM users "
                               "WHERE username = ?", (username,)).fetchone()
            return dict(row) if row else None

    def list_users(self):
        """Never returns hashes - nothing in the app needs them outside `verify_user`."""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT username, role, created_at FROM users ORDER BY username")]

    def verify_user(self, username, password):
        """Check a password. Returns the user on success, None on any failure.

        `compare_digest` rather than `==` so the comparison does not leak how much of the hash
        matched through its timing.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?",
                               (username,)).fetchone()
        if row is None:
            return None
        candidate = self._hash_password(password, row["salt"])
        if not secrets.compare_digest(candidate, row["password_hash"]):
            return None
        return {"username": row["username"], "role": row["role"]}

    def delete_user(self, username):
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            return conn.execute("DELETE FROM users WHERE username = ?",
                                (username,)).rowcount > 0

    def uses_default_passwords(self):
        """True while any seeded account still has its documented default password.

        Worth surfacing in the UI: a default password nobody changed is the most likely way
        this system gets embarrassed, and it is invisible unless something says so.
        """
        return [role for role, _env, default in DEFAULT_USERS
                if self.verify_user(role, default) is not None]

    # -------------------------------------------------------------------- sessions
    def create_session(self, username, role):
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("INSERT INTO sessions (token, username, role, created_at)"
                         " VALUES (?, ?, ?, ?)", (token, username, role, time.time()))
        return token

    def get_session(self, token):
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token = ?",
                               (token,)).fetchone()
            return dict(row) if row else None

    def delete_session(self, token):
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    # -------------------------------------------------------------- database browser
    # The administrator is meant to be able to see everything, so these two are generic. Table
    # names cannot be parameterised in SQL, so every caller's table name is checked against the
    # real list first - that check is what keeps this from being an injection hole.
    def table_names(self):
        with self._connect() as conn:
            return [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    def table_summary(self):
        return [{"name": name, "rows": self.table_count(name)}
                for name in self.table_names()]

    def table_count(self, table):
        if table not in self.table_names():
            raise ValueError(f"no such table '{table}'")
        with self._connect() as conn:
            return conn.execute(f"SELECT COUNT(*) AS n FROM \"{table}\"").fetchone()["n"]

    def table_page(self, table, limit=100, offset=0):
        """Read one page of a table. Newest first where there is an obvious id to sort on."""
        if table not in self.table_names():
            raise ValueError(f"no such table '{table}'")
        with self._connect() as conn:
            columns = [r["name"] for r in conn.execute(f"PRAGMA table_info(\"{table}\")")]
            order = "ORDER BY id DESC" if "id" in columns else ""
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM \"{table}\" {order} LIMIT ? OFFSET ?",
                (int(limit), int(offset)))]
        # Hashes and session tokens are the one thing an administrator has no reason to read,
        # and a screenshot of this page should not hand them over.
        redacted = {"password_hash", "salt", "token"}
        for row in rows:
            for key in row:
                if key in redacted and row[key]:
                    row[key] = "••• redacted"
        return {"table": table, "columns": columns, "rows": rows}

    # --------------------------------------------------------------------- session
    def reset_session(self):
        """Start a run with an empty history and every parcel back on its shelf.

        The simulator always restarts from the same world - all six parcels on their shelves,
        the robot docked and fully charged - so a database carrying yesterday's orders
        describes a warehouse that no longer exists. Worse, it would carry yesterday's stock
        levels, and after fix 3 a product delivered in a previous session would be missing from
        the catalogue of a world where it is visibly sitting on its shelf.

        DROP rather than DELETE, so a schema change (a new estimate column, say) lands cleanly
        on the next launch without needing a migration for tables nobody wanted to keep.
        """
        with self._connect() as conn:
            conn.executescript(
                "DROP TABLE IF EXISTS delivery_history;"
                "DROP TABLE IF EXISTS robot_telemetry;"
                "DROP TABLE IF EXISTS orders;"
                # robot_state goes too, so a column dropped from SCHEMA actually disappears
                # rather than lingering in an older file for ever. It holds one row of live
                # readings that the next telemetry sample replaces within a second.
                "DROP TABLE IF EXISTS robot_state;")
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO robot_state (id, last_update) VALUES (1, ?)",
                         (time.time(),))
            # Log everyone out too: a token from the previous run refers to a session nobody is
            # sitting at. Users themselves are NOT dropped - accounts outlive runs.
            conn.execute("DELETE FROM sessions")
            # Back to a docked, fully charged, undamaged robot. The live telemetry overwrites
            # this within a second of the state node starting, but until then the pages and the
            # admission gates must not be reading a flat battery from the previous run.
            conn.execute(
                "UPDATE robot_state SET battery_percent = 100.0, temperature_c = 22.0,"
                " condition_percent = 100.0, payload_kg = 0.0, motor_load = 0.0,"
                " cumulative_distance_m = 0.0, uptime_s = 0.0, state = 'charging',"
                " run_id = NULL, last_update = ? WHERE id = 1",
                (time.time(),))
            # One physical parcel per product in the world file, so one is the right restock
            # level - not whatever an administrator happened to leave behind.
            conn.execute("UPDATE products SET stock = 1")

    # ------------------------------------------------------------------- products
    def list_products(self, available_only=False):
        """Catalogue joined with its pick point, which is what an order actually needs.

        `available_only` hides products whose parcel is no longer on its shelf. Ordering one of
        those sends the robot to an empty pick point, where it fails three grab attempts and
        comes home - so the order form must not offer them at all.
        """
        query = ("SELECT p.*, k.x AS pick_x, k.y AS pick_y FROM products p "
                 "LEFT JOIN pick_points k ON k.pick_point_id = p.pick_point_id ")
        if available_only:
            query += "WHERE p.stock > 0 "
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query + "ORDER BY p.product_id")]

    def consume_stock(self, product_id, quantity=1):
        """Take a delivered product off the shelf. Never goes below zero.

        Called when an order COMPLETES, because at that moment the parcel is physically at the
        delivery bay. Admission gate 1 already refuses an out-of-stock product, so this is all
        it takes to stop the same parcel being ordered twice.
        """
        with self._connect() as conn:
            conn.execute("UPDATE products SET stock = MAX(0, stock - ?) "
                         "WHERE product_id = ?", (int(quantity), product_id))
        return self.get_product(product_id)

    def get_product(self, product_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.*, k.x AS pick_x, k.y AS pick_y,"
                " s.centre_x AS shelf_x, s.centre_y AS shelf_y FROM products p "
                "LEFT JOIN pick_points k ON k.pick_point_id = p.pick_point_id "
                "LEFT JOIN shelves s ON s.shelf_id = p.shelf_id "
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
                     reason=None, decided_by=None, kind=KIND_DELIVERY):
        """Insert a PENDING order. For a delivery, product and destination must both exist.

        The admission estimate is stored WITH the order so the report can compare what was
        predicted against what actually happened - and, while the order is unfinished, so that
        `committed_load` can hold its cost against the next decision.
        """
        if kind == KIND_DELIVERY:
            if self.get_product(product_id) is None:
                raise ValueError(f"unknown product '{product_id}'")
            if self.get_delivery_point(delivery_id) is None:
                raise ValueError(f"unknown delivery point '{delivery_id}'")
        else:
            product_id = delivery_id = None      # a return has neither
        estimate = estimate or {}
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO orders (kind, product_id, delivery_id, status,"
                " estimated_distance_m,"
                " estimated_energy_wh, estimated_return_energy_wh, estimated_peak_c,"
                " estimated_seconds, decision, decision_reason,"
                " decided_by, created_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, product_id, delivery_id, estimate.get("distance_m"),
                 estimate.get("energy_wh"), estimate.get("return_energy_wh"),
                 estimate.get("peak_temperature_c"), estimate.get("seconds"),
                 decision, reason, decided_by, time.time()))
            new_id = cur.lastrowid
        # Read back only AFTER the transaction commits - get_order opens its own connection
        # and would otherwise not see the new row.
        return self.get_order(new_id)

    def create_return_order(self, estimate=None, reason=None):
        """Queue a "go back to the station" task.

        Always accepted, and deliberately so: admission control exists to stop the robot taking
        work it cannot finish, but going home is the one job that makes a struggling robot
        BETTER off. A flat, hot robot being refused permission to go and charge would be
        exactly backwards.
        """
        return self.create_order(None, None, estimate=estimate, decision="accepted",
                                 reason=reason or "requested by the operator",
                                 decided_by="operator", kind=KIND_RETURN)

    def active_return(self):
        """The return task already queued or under way, if there is one.

        Guards against the obvious double-click: five queued returns would send the robot home
        once and then leave four tasks to complete trivially, which is just noise on /orders.
        """
        placeholders = ", ".join("?" * len(self.IN_FLIGHT_STATUSES))
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM orders WHERE kind = ? AND status IN ({placeholders}) "
                "ORDER BY id LIMIT 1",
                (KIND_RETURN, *self.IN_FLIGHT_STATUSES)).fetchone()
            return dict(row) if row else None

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

    # Statuses that mean "accepted, and the robot has not finished with it yet". An order in
    # any of these still owes energy and heat that has not been drawn from the pack, so its
    # estimate must be held against the robot when the NEXT order is judged.
    IN_FLIGHT_STATUSES = ("pending", "navigating", "grabbing", "delivering", "releasing")

    def committed_load(self):
        """The reservation ledger: work already promised but not yet paid for.

        Without this, two orders accepted while the robot is still busy are both judged against
        the same battery reading, so together they can commit the robot to more than it has -
        the classic admission-control mistake of checking capacity instead of REMAINING
        capacity. Energy sums (each order draws its own) while temperature takes the maximum
        (the motors get as hot as the hottest job, not the sum of them).
        """
        placeholders = ", ".join("?" * len(self.IN_FLIGHT_STATUSES))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS orders,"
                " COALESCE(SUM(COALESCE(estimated_energy_wh, 0)"
                "            + COALESCE(estimated_return_energy_wh, 0)), 0) AS energy_wh,"
                " MAX(estimated_peak_c) AS peak_temperature_c"
                f" FROM orders WHERE status IN ({placeholders})",
                self.IN_FLIGHT_STATUSES).fetchone()
        return {"orders": row["orders"] or 0,
                "energy_wh": float(row["energy_wh"] or 0.0),
                "peak_temperature_c": row["peak_temperature_c"]}

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
        """Aggregate statistics (proposal §3.1 A8: simple SQL, no custom algorithm).

        Every figure here counts DELIVERIES only. Returns are real orders and appear on
        /orders, but they are the operator moving the robot, not customer work: folding them in
        would let "success rate" be improved by sending the robot home a lot. They get their
        own two counters instead.
        """
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(status = 'completed') AS completed,"
                " SUM(status = 'failed')    AS failed,"
                " SUM(status = 'pending')   AS pending,"
                " SUM(status = 'cancelled') AS cancelled,"
                " SUM(status = 'refused')   AS refused"
                " FROM orders WHERE kind = ?", (KIND_DELIVERY,)).fetchone()
            returns = conn.execute(
                "SELECT COUNT(*) AS requested,"
                " SUM(status = 'completed') AS completed"
                " FROM orders WHERE kind = ?", (KIND_RETURN,)).fetchone()
            avg_row = conn.execute(
                "SELECT AVG(completed_at - created_at) AS avg_seconds FROM orders "
                "WHERE kind = ? AND status = 'completed' AND completed_at IS NOT NULL",
                (KIND_DELIVERY,)).fetchone()
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
            "returns_requested": returns["requested"] or 0,
            "returns_completed": returns["completed"] or 0,
            # Success rate is over FINISHED orders; counting pending ones as failures would
            # make the figure drift down simply because work is queued.
            "success_rate": round(completed / finished, 3) if finished else None,
            "average_delivery_seconds": (round(avg_row["avg_seconds"], 1)
                                         if avg_row["avg_seconds"] else None),
            "average_energy_wh": round(avg_energy, 2) if avg_energy else None,
            "average_distance_m": round(avg_distance, 2) if avg_distance else None,
            "total_energy_wh": round(energy["total_energy"], 2) if energy["total_energy"] else None,
        }
