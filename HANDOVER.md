# RoboFetch — Project Handover

Complete state of the project: what exists, how to run it, every problem hit and how it was
solved, and what remains. Written so a fresh session (or a new person) can pick it up cold.

**Last updated:** 2026-08-25 · **Status:** v2 + all requested work · **23 tests + 26/26 acceptance checks passing** · **COMMITTED**

## 0. START HERE — state of play and what to do next

Everything below this section is reference. This section is the working state.

### Where the project stands

The system works end to end and is verified: **23 pytest cases** (16 test functions, one
parameterised over the eight gated pages) and **26/26 acceptance checks** against the live
simulator, with every physical claim confirmed against Gazebo rather than against log messages.

> ### Everything is committed and merged to `main`
>
> A previous session left a day's work uncommitted while this banner claimed the opposite: the
> banner commit landed at 17:43 on 2026-08-18 and was accurate for twelve minutes, after which
> the battery capacity was halved, the test suite was trimmed, the report figures were rebuilt
> and `INSTALL.md` was written — none of it recorded. That backlog has now been verified and
> landed, and `chore/lifespan-migration` is merged.
>
> **The lesson, not the state:** a "what is committed" claim in this file goes stale the moment
> the next edit lands. Trust `git status`, never this paragraph.
>
> `git@github.com:ArianShafagh/robofetch.git`, branch `main`.

The **written report** is `report/RoboFetch_Software_Engineering_Report.tex`, about 2100 lines,
15 chapters and 2 appendices, with 40 figures (26 charts and diagrams, 14 screenshots). It has **never been compiled** — LaTeX is not
installed here; it targets Overleaf. See `report/README.md`. The three staleness items this
paragraph used to list are **done**: the UI screenshots have been retaken against the plain
redesign and the login page (12 new ones, including the e-stop and both admin pages), the
acceptance figure reads 26/26, and delivery accuracy is plotted at 0.18-0.49 m. All 40 figures
the `.tex` references exist on disk, so nothing renders as a `SCREENSHOT PENDING` box.

### What to do FIRST in a new session

1. `cat HANDOVER.md` — this section.
2. `git status` — should be clean or nearly so; see the banner above.
3. `./scripts/stop.sh --check` before launching anything (see §5.12; one stale process breaks
   every later launch).

### The owner's working style — read this, it saves rework

- **Verify against Gazebo ground truth, never against a log line or a status field.** This is the
  project's central rule. `./scripts/order.sh SKU-3001 delivery_1` does it automatically and ends
  in VERIFIED or NOT DELIVERED.
- **Measure before claiming a fix.** Two claims were made in the last session on bad measurements
  and had to be retracted; see §5.13 for the `top` trap specifically.
- **Do not add explanatory text to the web pages.** They show data and controls only; all guidance
  lives in README "The web app, page by page". `test_the_pages_carry_no_advice` enforces it.
- **The UI is deliberately plain.** No tiles, pills, cards, or decoration. Colour only where it
  carries meaning.
- **End every piece of work with exact steps the owner can run themselves.**
- The owner is learning: explain *why*, not only *what*.
- The report must contain **no dates, no schedule timings, no equations** — explicit requests.

### THE FOUR ORIGINAL REQUESTS — all four implemented and verified

**1. Battery drains faster, and accepted orders RESERVE their cost. — DONE**

*(a)* `CAPACITY_WH` in `robot_model.py` is now `11.0` — it went `40.0` -> `22.0` -> `11.0`, the
last step on 2026-08-18. A charge is now worth **one heavy delivery, or about three of the
lightest**. Measured from a full battery, counting the drive home as well as the outbound leg
(which is what `RESERVE_PERCENT` is judged against): the heaviest order, `SKU-3001` to
`delivery_1`, costs 6.13 Wh and leaves **34.8%**; the lightest, `SKU-3002` to `delivery_2`, costs
1.85 Wh and leaves **74.9%**. All twelve product/bay combinations are still accepted from full,
so nothing became unorderable — the robot simply recharges far more often.

Because the ML training label depends on how much of the pack an order consumes,
`tools/ml/generate.py` and `tools/ml/train.py` were re-run; `model.joblib` is trained against the
11 Wh pack. **If you ever change `CAPACITY_WH` again, you must regenerate and retrain.**

If you need to confirm which capacity a given `model.joblib` was built for, do not trust file
timestamps — back-solve `tools/ml/runs.csv` instead. Each row's battery drop covers the outbound
`energy_wh` plus an unloaded return leg that `generate.py` samples from 2.5-3.5 m, so only the
correct capacity puts that implied return distance inside the range.

*(b)* The reservation ledger is in. `orders` gained `estimated_return_energy_wh` and
`estimated_peak_c`; `Database.committed_load()` sums energy and takes the maximum peak
temperature over orders in `pending / navigating / grabbing / delivering / releasing`; and
`admission.project()` rolls the robot state forward past that before any gate runs. The ledger is
*derived* from the orders table rather than stored beside it, so a terminal status releases the
reservation automatically and the two cannot drift apart.

Refusals caused by the reservation say so ("…once the 2 order(s) already in progress are done"),
and `/preview` shows a "Battery at start" row when work is queued — otherwise the number
contradicts the battery on `/robot` and looks like a bug.

**2. Pages refresh every 2 seconds, and history is cleared on every start. — DONE**

`PAGE_REFRESH_SECONDS` is `2`, applied to `/orders` and `/robot` only. `Database.reset_session()`
DROPs and recreates `orders`, `delivery_history` and `robot_telemetry`, resets the `robot_state`
row to a docked, fully charged robot, and restocks every product; it is called from the FastAPI
startup event, so it happens however the system is launched. `Database.MIGRATIONS` additionally
`ALTER TABLE`s the new columns onto an old database file, for tools that open it without
resetting.

**3. A delivered product disappears from the list of options. — DONE**

`_on_status` calls `db.consume_stock()` when an order completes, so admission gate 1 refuses it
with "out of stock". `list_products(available_only=True)` backs the order-form dropdown and the
default `/api/products`; `/api/products?all=true` still returns the full catalogue for
administrators and for the acceptance suite. The order page keeps the delivered item visible in
the catalogue table at zero stock marked "delivered — not orderable", so its disappearance from
the dropdown is explained rather than mysterious.

**4. A parcel can no longer be dragged. — DONE**

Three layers, because this one corrupts every later delivery:

- `gripper_node.on_release` retries the detach up to `release_attempts` (3) times, verifying the
  joint's own state between attempts, and only clears `held_item` once the joint agrees;
- `task_manager.release_parcel` retries the whole service call, and **every** exit from
  `execute_order` after a successful grab now puts the parcel down first — including the
  "could not reach the delivery bay" path, which previously drove home still carrying it;
- if it still will not let go, `_parcel_is_stuck` fail-stops: the robot stays put, queued orders
  are failed with a clear reason rather than left pending for ever, and `_recover_stuck_parcel`
  keeps retrying so the robot heals itself the moment the joint reports honestly again.

Verified by fault injection — holding `/gripper/parcel_2/state` at `attached` reproduces the
original bug exactly, and the whole chain (retry → fail-stop → refuse queued work → recover)
behaves as designed. See "Verification evidence" below.

### ALSO ADDED — "Return the robot to its station" button

A queued task, not a command. `orders` gained a `kind` column (`delivery` | `return`), and for a
return `product_id` and `delivery_id` are NULL — there is no parcel, and the station is not a
delivery point.

- `GET /return` asks "Really send the robot back to its station?" and shows what the trip costs;
  `POST /return` queues it. Split that way so the thing that moves a robot is a POST a browser
  will never replay on refresh — the same reason `/preview` sits before `/confirm`.
- The button is on `/robot` and `/orders`. `POST /api/orders/return` is the REST equivalent.
- It goes out on the **same** `/orders/new` topic into the **same** FIFO queue, so it waits for
  the delivery in progress. A separate "drive home now" channel would let the operator interrupt
  a carry, and a parcel abandoned on the gripper is the exact failure fix 4 exists to prevent.
- **Never refused.** Admission control stops the robot taking work it cannot finish, but going
  home is the one job that leaves a struggling robot better off. It is still *costed* and still
  *reserved*, because the drive really does spend battery.
- Only one may be outstanding at a time (`Database.active_return()`), so a double-clicked button
  does not fill the queue.
- Analytics count deliveries only, with separate `returns_requested` / `returns_completed`
  counters — otherwise "success rate" could be improved by sending the robot home a lot.

Verified live: delivery queued, return requested immediately after, return sat at `pending`
through `delivering` and `releasing`, then ran as soon as the delivery completed and ended
`at the station, charging` with the robot state `charging`. A second request returned HTTP 409.
Delivery success rate stayed 1/1 with returns reported as 1/1 separately.

### ALSO ADDED — login, roles, emergency stop, admin CRUD, database browser

**Login and two roles.** `users` and `sessions` tables; PBKDF2-SHA256 with a per-user salt
(stdlib `hashlib`, no new dependency). The cookie holds only a random token, so there is no
signing key to leak — and `itsdangerous` is not installed, which ruled out Starlette's
`SessionMiddleware` anyway. Sessions are cleared by `reset_session()` on every launch; accounts
are not.

| Role | Reach |
|---|---|
| `controller` | order, track, robot condition, return to station, emergency stop |
| `admin` | all of the above, plus product and delivery-point CRUD, the database browser, and user management |

Seeded as `admin`/`admin` and `controller`/`controller`, overridable via
`ROBOFETCH_ADMIN_PASSWORD` / `ROBOFETCH_CONTROLLER_PASSWORD` **before first launch**. While a
default is still in use the API prints a warning **to the launch console** — the pages carry no
advice of any kind, so that is the one place it is said.

**Emergency stop — one button, one shot.** No confirmation (a stop that asks "are you sure?" is
not an emergency stop) and **no latch**. `POST /estop` publishes on `/robot/estop`; the task
manager handles it **on the subscription callback**, not the worker thread, because the worker is
normally blocked waiting on a navigation result when the button is pressed. It cancels the
in-flight Nav2 goal, brakes with zero `/cmd_vel`, fails the running order, and fails everything
queued. Then it is over — the robot stands where it stopped and is immediately ready for new work.

`TaskManager._abort` is a transient `Event`, raised by the button and cleared by `_serve_queue`
the moment the worker regains control. Nothing is written to the database, so **nothing survives
a logout** — an earlier latched version did, which was the bug that prompted this design.

It does not drive home and does not put a carried parcel down: the robot is left alone, full
stop. Going home afterwards is a separate, deliberate press of "Return to station". Because a
stop can leave a parcel on the gripper, `_serve_queue` calls `_drop_carried()` before starting
**any** task — otherwise the operator's next order would drag it, which is the bug fix 4 exists
to prevent.

The button sits alone at the far right of the header (`.estop-slot { margin-left: auto }`) with
3 rem of dead space to its left. That spacing IS the safeguard against an accidental press; there
is deliberately no confirmation step to slow down a real one.

**Admin CRUD and the database browser.** `/admin/products` and `/admin/delivery-points` add,
update and delete through the existing `upsert_*` / `delete_*` methods. `/admin/db` browses every
table read-only, 100 rows a page, newest first. Password hashes, salts and session tokens are
redacted. Table names cannot be parameterised in SQL, so every requested name is checked against
`sqlite_master` first — that check is the injection defence, and `test_db.py` pins it.

**phpMyAdmin does not apply.** It speaks to MySQL; this is SQLite, a single file with no server.
The equivalent is `sqlite-web`, documented on `/admin/db` and in the README.

**The UI is now plain, and carries no guidance.** `style.css` is ~130 lines of variables and
thin rules: no tiles, pills, cards, gradients or rounded corners. Colour survives only where it
carries meaning. Every explanatory paragraph has been removed from every page — the pages show
data and controls only, and all of that guidance now lives in README "The web app, page by page".
`test_the_pages_carry_no_advice` fails the build if advice, tool names or credentials reappear.

Two display details worth keeping:

- **Links are pinned to `color: inherit` in all five states**, so nothing is browser-blue and a
  visited link never turns purple. Underlines carry the affordance instead of colour.
- **The stylesheet is cache-busted by its own mtime** (`style.css?v=<mtime>`). Without it a
  browser happily serves the previous design and the redesign looks like it never deployed —
  which is exactly what happened once during development.

There is a `prefers-color-scheme: dark` block, so the page follows the operating system; in dark
mode `--fg` is white and the status colours are lifted (`#060` on `#111` is unreadable).

**The 7 report screenshots are now stale and need retaking.**

**Known limitation, deliberate:** only the HTML pages are gated. The REST API is open, because
`scripts/acceptance.py` and the helper scripts are unauthenticated clients of it and this is a
single-user simulator on one machine. If it ever faced a network, `/api/*` would need the same
session check and every script would need a token. Recorded in README "Known limitations".

### ALSO FIXED — grab geometry, so deliveries land ON the bay

Deliveries used to end 0.6-0.8 m from the bay marker. The cause was at **grab** time, not
release: the robot parked without facing the parcel (`navigate_to` defaults to `yaw=0.0`), the
gripper accepted anything within 0.85 m in **any** direction, and `DetachableJoint` carries and
releases a parcel at whatever offset it had when attached — so the drop inherited the grab error.

- `TaskManager.approach(point, target, standoff)` drives to a point **facing** something. At the
  pick point it faces the shelf; the shelf centre is sent with the order.
- The gripper refuses a grab outside `grab_half_angle_deg` (60) or nearer than
  `min_grab_distance` (0.15 m).
- The delivery approach stops `carry_offset` (0.55 m) **short** of the bay, facing it, so the
  carried parcel lands on the bay rather than past it.
- `delivery_tolerance` 1.1 -> **0.7**, and `DELIVERY_TOLERANCE` in the acceptance suite with it.

Measured: **0.24, 0.31, 0.44, 0.49 m** across all three shelves and both bays.

Full detail, plus two approaches that FAILED and must not be retried, in §5.11b.

### STILL OPEN — pick these up next

1. **The robot bulldozes parcels it has already delivered** — see immediately below. Not fixed;
   three candidate fixes listed, all of which change the warehouse layout figure in the report.
2. **`/clock` runs at ~500 Hz and dominates CPU** (§5.13). Not needed for correctness — the suite
   passes 26/26 headless. It matters only for making the **GUI/RViz** run comfortable, which is
   the owner's remaining pain point. Three levers listed, none tested; one experiment already
   produced a failed launch and was reverted.
3. **Multi-item stock is deliberately NOT implemented.** The owner decided the catalogue is the
   system of record and the simulator is only a demo: if the database says a shelf holds four of
   something and Gazebo has one, ordering the missing ones simply fails after three grab attempts,
   and that is acceptable. Do not build Gazebo-to-database syncing; it was considered and rejected
   as too heavy. Recorded under Known limitations in the README.

### NEWLY FOUND — the robot bulldozes parcels it has already delivered

This is **not** one of the four, and it is **not fixed**. It is very likely what was actually
observed as "a parcel was dragged for the whole run".

The station is at `(0, -2.2)` and both delivery bays are at `(-3.0, -2.2)` and `(2.6, -2.2)` —
all three on the same line. Parcels sit below the lidar plane, so Nav2 cannot see them. After
delivering, the robot drives home along that line and pushes the parcel it has just dropped.
Observed directly: `parcel_5` was verified delivered 0.66 m from `delivery_1`, and after the
robot returned to the station it was at `(+0.26, -2.05)` — shoved 2.6 m, sitting on the dock.

It is a push, not a drag: the joint reported `detached`, C3 verification passed before the robot
moved, and the parcel ended 0.30 m from the robot centre rather than at the 0.55 m carry offset.

Options, cheapest first:

- move the station off the line, e.g. to `(0.0, -0.6)`, so the return path does not cross either
  bay. One row in `DEFAULT_STATION`, plus `initial_pose`/`station` in `delivery.launch.py` and
  the acceptance `SPAWN` expectations — and the report's warehouse figure;
- or spread the drop points, giving each bay a small offset per delivery so parcels do not pile
  up on the through-route;
- or route home via a waypoint north of the bay line.

Decide before re-running the report figures, since it changes the warehouse layout diagram.

### Verification evidence for the four fixes

Every claim below was checked against the simulator, not against a log message.

| Fix | Evidence |
|---|---|
| 1a faster drain | `SKU-3001` to `delivery_1` predicted "battery 67.5% after", against ~82% on the old pack |
| 1b reservation | live battery 82.1%, two orders in flight holding 9.5 Wh → next order costed from **38.9%** (82.1 − 9.5/22×100 = 38.9, exact) |
| 2 reset | relaunched against a 143 KB `robofetch.db` carrying the previous session; `/api/orders` came back `[]` and every product restocked |
| 3 stock | after delivering `SKU-3001`, `/api/products` returned 5 items, `?all=true` still 6 with stock 0, re-ordering refused "SKU-3001 is out of stock" |
| 4 release | fault-injected `/gripper/parcel_2/state` held at `attached`: gripper retried 3×, task manager retried 2×, order failed with the stuck reason, the **queued order was failed rather than run**, and the robot recovered by itself once the joint reported `detached` |

Fault injection for fix 4, which is worth keeping — it reproduces the original bug on demand:

```bash
# terminal 1: make the gripper believe the joint never lets go
ros2 topic pub -r 5 /gripper/parcel_2/state std_msgs/msg/String "data: attached"
# terminal 2: order that parcel, and one behind it
./scripts/order.sh SKU-1002 delivery_1
# stop terminal 1, then tell the truth again and watch it recover
ros2 topic pub -1 /gripper/parcel_2/state std_msgs/msg/String "data: detached"
```

### How to work on this

The owner's standing preferences are in §9. The ones that matter most:

- **Verify against Gazebo ground truth**, never against logs. `./scripts/order.sh SKU-3001
  delivery_1` does this automatically and is the fastest way to check a change.
- **Always `./scripts/stop.sh` before relaunching.**
- **Test with the GUI as well as headless** — though note that on this machine the GUI run
  currently cannot localize at all, because Gazebo and RViz together starve the sensor pipeline.
  Headless is reliable; that limitation is documented in the report.
- **End every piece of work with exact steps the owner can run themselves.**
- The owner is learning: explain *why*, not only *what*.
- The report must contain **no dates and no schedule timings**, and **no equations** — those were
  explicit requests.

### Quick verification after any change

The acceptance suite shells out to `gz`, so **source ROS in the shell you run it from** or every
parcel reports "no pose from Gazebo" and it blames the world instead of the shell (see §4).

```bash
cd ~/robofetch_ws && source /opt/ros/jazzy/setup.bash && source install/setup.bash
./robofetch_venv/bin/python -m pytest src/robofetch_core/test/ src/robofetch_bridge/test/ -q   # 23
./scripts/stop.sh && ./scripts/run.sh --headless        # wait for "Localized and ready"
./scripts/order.sh SKU-3001 delivery_1                  # ends with VERIFIED or NOT DELIVERED
./robofetch_venv/bin/python scripts/acceptance.py       # 26/26, needs a fresh world
                                                        # ~12 min: it waits for recharges
```

---

> **2026-08-07 — RoboFetch v2 is implemented and verified.** Full acceptance suite passes
> **15/15** against the live simulator, every physical claim confirmed against Gazebo ground
> truth. 65 pytest tests pass.
>
> v2 replaced the v1 feature set after the professor approved `docs/proposal.md`:
> - Orders are placed by **product ID** against a real catalogue; coordinates live in the
>   database, not in code.
> - The robot has a **condition model** — battery, temperature, wear — logged to a per-run CSV.
> - Every order goes through **admission control**: cost estimate, a scikit-learn classifier,
>   and policy gates that can overrule it. The robot **refuses** work it cannot do.
> - The web app is **server-rendered HTML with no JavaScript**; the live map is gone.
> - The nearest-neighbour scheduler and the retry FSM were **removed** (FIFO by order time; a
>   plain 3-attempt grab, then return to station).
>
> Read `docs/proposal.md` first - it is the approved specification this code implements.

---

## 1. What this is

A simulated **pick-and-delivery robot**. A client orders a product by catalogue ID through a web
application; the system predicts whether the robot can do the job in its current condition and
either accepts or refuses it. If accepted, a differential-drive robot in Gazebo navigates the
warehouse, picks the parcel up, carries it to a delivery bay, drops it, and goes home to charge.

Built from a university software-engineering proposal as a **learning project** — the owner wants
to understand every part, not just have it work. The approved v2 specification is
`docs/proposal.md`; read that first.

### Architecture

```
   Browser (server-rendered HTML, no JavaScript)
        │  form posts
   ┌────▼──────────────────────────┐        ┌──────────────────────┐
   │  FastAPI + SQLite             │───────▶│  robofetch_ai :8001  │
   │  + ADMISSION WORKFLOW         │◀───────│  feasibility model   │
   └────┬──────────────────────────┘        └──────────────────────┘
        │  ROS 2 topics (JSON on std_msgs/String)
   ┌────▼──────────────────────────┐
   │  Task manager (FIFO)          │   robofetch_core
   │  Robot condition model        │   ← OUR custom logic
   │  Gripper node                 │
   └────┬──────────────────────────┘
        │  actions / services
   ┌────▼──────────────────────────┐
   │  Nav2  +  Gazebo Harmonic     │   third-party, used as-is
   └───────────────────────────────┘
```

### Proposal mapping (important for the report)

| Proposal section | What satisfies it |
|---|---|
| §3.1 Data/CRUD | `robofetch_bridge/db.py` — products, shelves, pick points, delivery points, station, orders, history, telemetry |
| §3.2 Third-party | Nav2, Gazebo, FastAPI, SQLite, scikit-learn, pandas, joblib |
| §3.3 **Complex C1** | Robot condition model + duty cycle — `robofetch_core/robot_model.py`, `robot_state_node.py` |
| §3.3 **Complex C2** | Order admission + cost preview — `robofetch_bridge/admission.py` |
| §3.3 **Complex C3** | Physical delivery verification — `task_manager.execute_order` step 5 |
| FR8 (3 grab attempts) | `task_manager.execute_order` — acceptance-verified |
| NFR1 (<2 s preview) | measured **416 ms** in the recorded run — acceptance check |
| NFR2 (AI-down fallback) | `predictor.py` returns None → policy decides alone — unit-tested |

---

## 2. Environment

- **OS:** Windows 11 + WSL2, Ubuntu 24.04
- **ROS 2 Jazzy**, **Gazebo Harmonic (gz-sim 8)** — the modern `gz sim`, **not** Gazebo Classic
- **Workspace:** `~/robofetch_ws`
- **venv:** `~/robofetch_ws/robofetch_venv`, created with `--system-site-packages` so FastAPI and
  `rclpy` are importable in one process. `rclpy` only appears after
  `source /opt/ros/jazzy/setup.bash` (it lives on `PYTHONPATH`, not in site-packages).

### Deviations from the proposal (methodology unchanged, only the tools)

| Proposal said | We use | Why |
|---|---|---|
| Gazebo Classic "link attacher" | Gazebo Harmonic **`DetachableJoint`** | Classic is EOL and unavailable on Jazzy; same attach/detach concept |
| Husarion ROSbot | custom diff-drive URDF + 2D lidar | zero external deps, fully understood, reliable |
| (maze) | **warehouse**: open room, 3 shelves, 1 delivery station | the maze's symmetry broke localization — see §5.3 |
| SLAM-built map | map **generated from world geometry** | proposal §2.2 says *known map*; §7 lists SLAM as a *future extension*. SLAM maps drifted badly |

---

## 3. Packages

| Package | Contents |
|---|---|
| `robofetch_description` | Robot URDF/xacro, Gazebo plugins, RViz config |
| `robofetch_gazebo` | `warehouse.sdf` world, `bridge.yaml` (gz↔ROS), sim launch |
| `robofetch_nav` | `nav2_params.yaml`, generated map, navigation launch |
| `robofetch_interfaces` | `Grab.srv` |
| `robofetch_core` | **our logic**: `robot_model.py`, `robot_state_node.py`, `task_manager.py`, `gripper_node.py` |
| `robofetch_bridge` | **web tier**: `app.py`, `db.py`, `admission.py`, `predictor.py`, `ros_link.py` |
| `robofetch_ai` | feasibility classifier service on port 8001 |
| `robofetch_bringup` | `delivery.launch.py` — starts everything |
| `robofetch_web` | Jinja2 templates + one stylesheet (no JavaScript) |
| `tools/ml` | **disposable** synthetic data generator + trainer (outside the build) |

### The world (`warehouse.sdf`) — 8 × 6 m room

```
 ┌────────────────────────────────────────┐  y=+3
 │▓▓▓▓ shelf_1 ▓▓▓▓      ▓▓▓ shelf_2 ▓▓▓  │  ← both flush to the north wall
 │   ▪1001   ▪1002        ▪2001  ▪2002    │
 │                                   ▓▓▓▓ │
 │                             ▪3002 ▓ s3 ▓  ← flush to the east wall
 │                             ▪3001 ▓    ▓
 │  ◎delivery_1     ⌂station      ◎delivery_2
 └────────────────────────────────────────┘  y=-3
 x=-4                                    x=+4
```

Map frame == Gazebo world frame, so these are literal world coordinates. **All coordinates now
live in the database**, not in code: `shelves`, `pick_points`, `delivery_points`, `station`.

| Name | Coordinates | | Name | Coordinates |
|---|---|---|---|---|
| `pick_1a` / `pick_1b` | (−3.0, 0.95) / (−2.0, 0.95) | | `delivery_1` | (−3.0, −2.2) |
| `pick_2a` / `pick_2b` | (1.05, 0.95) / (1.95, 0.95) | | `delivery_2` | (2.6, −2.2) |
| `pick_3a` / `pick_3b` | (2.75, −1.5) / (2.75, −0.5) | | `station_1` | (0.0, −2.2) |

Six parcels, two per shelf, ~1 m apart. Parcels are 0.09 m cubes — **deliberately below the
0.13 m lidar**, so a carried one is never mistaken for an obstacle. They are carried on the floor,
**not lifted**: raising one into the laser plane would make the robot see an obstacle directly in
front of itself and refuse to move, so the spacing is what stops them colliding.

The two delivery bays are in **opposite corners** and the station is somewhere else again, so the
robot never parks on a parcel it has already delivered.

---

## 4. How to run

### Cold start (one command, from a fresh terminal)

```bash
cd ~/robofetch_ws && ./scripts/run.sh
```

Stops leftovers → sources ROS → builds → sources workspace → launches Gazebo + RViz + Nav2 + robot
nodes + the API on port 8000. Verified working from a completely unsourced shell.

Options: `--no-build`, `--headless`, `--retry-demo`.

### Manual equivalent

```bash
cd ~/robofetch_ws
source /opt/ros/jazzy/setup.bash        # ROS itself
colcon build --symlink-install
source install/setup.bash               # our packages — BOTH sources needed, in this order
ros2 launch robofetch_bringup delivery.launch.py
```

### Order something (second terminal)

Open **http://localhost:8000** in the Windows browser — that bypasses WSLg entirely, so it works
even when Gazebo's own window does not. Pick a product and a destination, read the cost and the
verdict, confirm. Pages are server-rendered; there is no JavaScript.

From a terminal:

```bash
./scripts/order.sh SKU-3001 delivery_2
```

Prefer this over a bare `curl`. It checks the failure modes in §5.9 *before* submitting, shows the
admission verdict and its reasoning, prints every state change, and then compares the parcel's
**real Gazebo pose** against the bay — so a `completed` status is never taken on trust.
`./scripts/order.sh --list` prints the catalogue.

The REST API is still there for scripts: `POST /api/preview`, `POST /api/orders`,
`GET /api/orders/{id}`, `GET /api/robot`, `GET /api/analytics`, `GET /health`.

---

### Other scripts

| Script | Purpose |
|---|---|
| `./scripts/order.sh [SKU] [delivery]` | **The way to submit an order** — see above |
| `./scripts/stop.sh` | **Run this whenever a launch didn't exit cleanly** — see §5.1 |
| `./scripts/goto.sh shelf1\|shelf2\|shelf3\|delivery` | manual navigation goal |
| `./scripts/gripper.sh grab\|release` | manual grab/release |
| `./scripts/teleop.sh` | keyboard driving |
| `./scripts/set_pose.sh` | re-localize the robot |
| `./scripts/generate_map.py` | regenerate the map **after any world geometry change** |
| `./scripts/acceptance.py` | the full acceptance suite (15 checks) |
| `tools/ml/generate.py`, `tools/ml/train.py` | **disposable** — rebuild the classifier |

### Tests

```bash
cd ~/robofetch_ws && source install/setup.bash
./robofetch_venv/bin/python -m pytest src/robofetch_core/test/ src/robofetch_bridge/test/ -v
```
**23 cases passing** from 16 test functions — the gate test is parameterised over the eight
gated pages. Covers the condition model, admission policy (including the reservation ledger), the
database (stock, session reset, the browser's injection defence and redaction), and
HTTP↔SQLite↔ROS integration.

The suite is deliberately a **small representative set**, not exhaustive: it was trimmed from 138
functions on 2026-08-18 so the live acceptance suite carries the weight instead. That is why the
test pyramid in the report is inverted — the physical layer is the widest tier.

Acceptance, against the live simulator (needs a freshly launched stack):

```bash
./robofetch_venv/bin/python scripts/acceptance.py          # --quick skips the slow physical ones
```
**26/26 passing.**

The check IDs were renumbered on 2026-08-18 to match the requirement numbering used in the report:
`UC5`->`UC1`, `UC1`->`UC3`, `UC2`->`UC4`, `UC4`->`UC5`, `FR3`->`FR16`. Same checks, same
assertions — only the labels moved, so an old log compared against a new one will look like
checks changed when they did not.

### ⚠️ Verify with Gazebo, never with logs alone

```bash
source /opt/ros/jazzy/setup.bash
gz topic -e -t /world/warehouse/dynamic_pose/info -n 1 | grep -A6 'name: "item_'
```
This matters — see §5.4. The same command with `name: "robofetch"` gives the robot's true pose, which
is the only way to catch AMCL drifting (see §5.5).

**Run the acceptance suite from a shell that has sourced ROS**, or it fails for the wrong reason.
`scripts/acceptance.py` shells out to `gz topic`, which inherits your environment. The `gz` binary
is on `PATH` without sourcing anything, but its **subcommands** are not: without `GZ_CONFIG_PATH`
it prints *"I cannot find any available 'gz' command"* and **exits 0**. The suite reads that empty
output as no pose, and the SETUP check reports:

```
FAIL  SETUP    parcels are on their shelves (fresh world)
      parcel_1: no pose from Gazebo; ... - restart with ./scripts/stop.sh && ./scripts/run.sh
```

which accuses the world of being dirty when the world is perfectly fine and the *shell* is wrong.
The tell is that **all six** parcels report no pose at once - a genuinely stale world has moved
one or two, never all six. Confirm with `echo $GZ_CONFIG_PATH` before believing the message.

**Do not use `/amcl_pose` as a readiness check.** Its publisher is `TRANSIENT_LOCAL` (latched), so
`ros2 topic echo /amcl_pose --once` returns the last stored message *instantly and forever* — even
when AMCL has been silent for minutes. It looks like a live signal and is not one. AMCL also only
publishes when the robot moves, so `topic hz` is quiet on a stationary robot. The trustworthy
readiness signal is the task manager's own log line `Initial pose (...) accepted by AMCL`.

---

## 5. Problems hit and how they were solved

This is the most valuable part of the document. Grouped by theme.

### 5.1 Tooling and environment

**Gazebo/RViz windows opened blank or not at all (WSLg).**
`/mnt/shared_memory` was missing, a known WSL bug (microsoft/WSL#40618). WSLg silently fell back to
"COPY MODE" and delivered no pixels — window titles literally showed `[WARN:COPY MODE]`. Fixed by
adding to `/etc/fstab`:
```
tmpfs /mnt/shared_memory tmpfs defaults 0 0
```
then `wsl --shutdown`. The mode is negotiated once at WSLg start, so mounting inside a running
session does not help.

**Gazebo's GUI window still never appeared when launched via `ros2 launch`.**
gz-gui is Qt Quick; on WSLg its Wayland window never registers. `QT_QPA_PLATFORM=xcb` fixes it and is
now set *inside* `sim.launch.py` via `SetEnvironmentVariable`, so it works however you launch. RViz
uses Qt Widgets and was never affected — which is why RViz worked while Gazebo didn't.

**Leftover processes silently corrupt the next run.**
A killed launch leaves Gazebo publishing `/clock`. Two clock sources → *"Detected jump back in time"*
→ TF chaos → Nav2 aborts every goal with confusing errors. This wasted hours. **Always run
`./scripts/stop.sh` before relaunching.**

**`pkill -f` kills the agent's own shell** in this environment (exit 144). Kill by PID or process
group only.

**`stop.sh` did not stop the web API** (found and fixed 2026-08-05). It matched processes by name,
and the API's process name is just `python` (it runs as `python -m uvicorn ...`) — far too generic
to list. So every run left the API alive. The next launch's API then died instantly with
*"[Errno 98] address already in use"* while `curl localhost:8000` kept answering — from the **stale**
process, against **whatever database it was started with**. Orders were accepted and never executed,
and every symptom pointed at the robot instead of at the port. One orphan survived nearly an hour
across sessions this way. `stop.sh` now also matches the API on its full command line.

Diagnose it with: `ss -lptn 'sport = :8000'` and `grep "address already in use"` in the launch log.

### 5.2 Robot model and Gazebo

**URDF→SDF "lumps" fixed-joint links into their parent**, so `gripper_link` vanished and
DetachableJoint reported *"Link with name gripper_link not found"*. Fixed with
`<preserveFixedJoint>` + `<disableFixedJointLumping>` on `gripper_joint`.

**DetachableJoint is created ALREADY ATTACHED** at startup, with no "suppress initial attach"
option. The gripper node therefore publishes one detach per item at start-up.

**THE nastiest bug of the project:** the gripper only released `item_1` at start-up, so `item_2` and
`item_3` stayed welded to the gripper *from metres away* and **anchored the robot**. The wheels
turned and odometry advanced while the robot never moved in Gazebo — and every downstream signal
(odom, AMCL, RViz) agreed with the lie. Fixed by an `items` parameter listing every item with a
DetachableJoint, all released at start-up.

**Robot skidded and odometry was corrupted.** Wheels were offset behind the centre of mass, so ~43%
of the weight sat on a frictionless caster. Fixed: wheels directly under the CoM, two casters, real
ground clearance, higher wheel friction. Odometry error is now ~0.5%.

**`ros_gz_bridge` drops per-entity names** converting `gz.msgs.Pose_V` → `TFMessage`, so bulk pose
topics are useless for identifying models. Each model therefore carries its own `PosePublisher`
(`/model/<name>/pose`). Leave `static_publisher` **false** — setting it true stops publication
entirely.

**The robot's own PosePublisher goes quiet** (it emits only on change), which once left the gripper
comparing against a stale start-of-run position and reporting an item 5 m away when it was 0.3 m.
The gripper now takes the robot position from `/amcl_pose`, which publishes continuously.

### 5.3 Localization — the big conceptual one

**Symptom:** in RViz the laser dots matched the walls perfectly, then "the entire world suddenly
changed". The robot then drove confidently into walls.

**Cause: perceptual aliasing.** The old maze was a **6 × 6 square** room. Rotated 90°, the predicted
laser scan is *identical* — AMCL genuinely cannot tell those poses apart, so the particle cloud would
collapse onto a wrong hypothesis and the `map→odom` correction jumped. Because RViz draws in the map
frame, the map stays put and the robot+scan teleport — hence "the world changed". The global plan was
always *correct for the pose AMCL believed*; the belief was wrong.

**Fix — the warehouse redesign:**
1. **Rectangular room (8 × 6)**, not square — removes the rotational ambiguity.
2. **Three shelves**, different sizes/orientations, asymmetrically placed, 0.8 m tall (well above the
   lidar) — every part of the room now produces a distinctive scan. *The pickup points and the
   localization fix are the same objects doing double duty.*
3. **AMCL recovery enabled** (`recovery_alpha_slow/fast` non-zero) so it can re-scatter particles
   instead of staying confidently wrong.

**Result:** AMCL error went from *metres, with jumps* to **0.05–0.14 m, no jumps**.

### 5.4 Trusting the wrong evidence — a process lesson

The task manager once reported **"3/3 delivered"** while the parcels had **never moved in Gazebo**.
Every command returned success; the verification only checked proximity, which passes trivially when
nothing happened.

**Fix:** `execute_order` now checks each parcel's **real world pose** against the drop-off before
marking COMPLETED. Reported outcomes and physical reality cannot diverge.

**Rule adopted:** verify with `gz topic ... dynamic_pose/info`, never with `/odom`, AMCL, RViz or log
messages alone — all of those were downstream of the corrupted data at various points.

### 5.5 Navigation and Nav2

**`route_server` fails to configure** because `nav2_bringup` supplies no graph file — and that
failure **aborts the entire Nav2 bringup**. We don't use the route server; it just needs
`graph_filepath` set in `nav2_params.yaml`.

**`ActionClient.wait_for_server()` returns before Nav2's lifecycle nodes are ACTIVE**, and an
inactive server *rejects* goals. Wait for an `/amcl_pose` message as the real readiness signal, and
retry rejected goals.

**AMCL silently discards the initial pose** if its TF buffer is not ready — it then repeats *"AMCL
cannot publish a pose... Please set the initial pose"* forever and the robot never moves. Publishing
a fixed number of times is not enough: `_publish_initial_pose` now **retries until `/amcl_pose`
confirms**.

**MPPI is CPU-hungry.** With Gazebo's GUI + RViz on WSL the default (batch 2000 × 56 steps) starved
the control loop; the robot stalled and goals aborted. Reduced to **600 × 32 at 10 Hz**. Note MPPI
*requires* controller period ≤ `model_dt`, so `controller_frequency` and `model_dt` must be changed
**together** — changing only the frequency makes `controller_server` refuse to configure and aborts
the launch.

**Tight gaps trap the robot.** A 0.44 m robot with 0.35 m costmap inflation cannot use a 0.9 m gap.
`shelf_3` is now flush against the east wall so no dead-end exists. **Never leave gaps under ~1.2 m.**

**The same trap once existed behind `shelf_1` and `shelf_2` — found 2026-08-05, FIXED.** The rule
above had only ever been applied to `shelf_3`. Both shelves used to end at y = 2.05 while the north
wall's inner face is at y = 2.95, leaving a **0.90 m corridor** across the whole north side,
reachable through the 2.25 m gap between them.

The diagnosis is the part worth keeping, because the symptom pointed nowhere near the cause.
Observed live: the robot drove in, wedged itself, and AMCL's belief diverged from Gazebo ground
truth by **2.85 m** — it reported (−2.03, −0.35) while the robot was really at (−1.60, 2.50),
inside that corridor. Because the scan was then taken from behind a shelf while the costmap was
written around the *believed* pose, the robot appeared **enclosed**: the global planner failed with
*"Failed to create plan with tolerance of 0.500000"* for **every** goal, including the open centre
of the room. Costmap clears and the wait-recovery did not help.

**Fixed** by extending both shelves north to be flush with the wall, exactly as `shelf_3` is flush
with the east wall, and re-running `scripts/generate_map.py`. Verify it in one line rather than
trusting this paragraph — geometry is cheap to measure:

```bash
grep -A3 'name="shelf_1"' src/robofetch_gazebo/worlds/warehouse.sdf | grep -E 'pose|size'
```

`shelf_1` and `shelf_2` sit at y = 2.25 with depth 1.4, so they span 1.55 → **2.95**; `wall_north`
is at y = 3.0 with thickness 0.1, so its inner face is **2.95**. Flush, no corridor.
`scripts/generate_map.py` carries the same three rectangles and a comment saying why all three are
flush — **keep the two in sync, or the map will disagree with the world.**

This entry used to claim the trap was "very likely the real identity of the occasional goal abort"
in §7. That attribution never held: goal aborts under load are the CPU-starvation issue in §5.13,
and they persisted after the shelves were moved.

**Parcels piling up on one drop-off point** made the robot drive into an invisible parcel it had
already delivered (they're below lidar height). Drop-offs are spread apart.

**Dragging a parcel stalled the robot.** DetachableJoint welds a parcel wherever it lies — often
0.5 m off-centre — and at normal friction dragging it resisted enough for Nav2's progress checker to
abort. **Parcels were given near-zero friction (mu 0.02). This single change took deliveries from
1/3 to 3/3.**

### 5.6 Launch-system traps

**`IncludeLaunchDescription` leaks its arguments into the parent scope.** Passing `rviz:=false` to
the sim include silently switched off the *parent's* RViz too. Wrap includes in
`GroupAction(scoped=True)`.

**Launch cannot parse a URDF as YAML.** `robot_description` must be wrapped:
`ParameterValue(Command(["xacro ", path]), value_type=str)`.

**ROS 2 cannot infer the type of an EMPTY array parameter.** Launching with no pre-queued orders
aborted the whole launch with *"Expected a non-empty sequence... inconsistent input"*. `orders` is
therefore a **semicolon-separated string**, not a list.

**Nav2/slam_toolbox nodes are LIFECYCLE nodes.** Launched raw they sit "unconfigured", declare no
parameters and publish nothing — they look alive but do nothing. Use their own launch files, which
emit the configure/activate transitions.

### 5.7 Web tier

**SQLite: reading a row back through a second connection before the insert commits returns nothing.**
`create_order` must commit, *then* read.

**`StaticFiles` refuses to serve symlinked files, which `--symlink-install` creates.** Starlette
resolves each request with `os.path.realpath` and rejects anything landing outside the mounted
directory — a sound path-traversal guard, but `colcon build --symlink-install` installs
`share/robofetch_web/web/*` as symlinks back into `src/`, so every asset was rejected.

The failure is deceptive: `/` kept working (it is a `FileResponse`, which does no such check) so the
page loaded, while `/app/style.css` and `/app/app.js` both returned **404** — an unstyled, dead page
that looks like a frontend bug. Fixed with `StaticFiles(..., follow_symlink=True)` in `app.py`.

The upside of those symlinks: editing `src/robofetch_web/web/*` takes effect on a browser reload,
with no rebuild.

### 5.8 The gripper node wedged solid — rclpy executor saturation

Found while building M8. Symptom: `~/grab` never returned, so the task manager burned three
180-second service timeouts and failed the order. The gripper node looked *alive* — it was using 96%
CPU — but it had stopped dispatching **everything**, including `ros2 param get`, which is handled by
rclpy internals and has nothing to do with our code.

Diagnosis, from `/proc/<pid>/task/*/stat`: the **main thread was in state `R`** (spinning in Python,
holding the GIL) while all 26 other threads sat in `futex_do_wait`. The `MultiThreadedExecutor` was
consuming its whole time budget in `wait_for_ready_callbacks` and never handing work to its threads.

Cause: **too many messages for one Python executor.** Four `PosePublisher`s — the robot plus three
parcels — each at `<update_frequency>20</update_frequency>`, all subscribed by the gripper node, is
~70 Python callbacks per second on top of `/amcl_pose`. rclpy cannot keep up, and once it falls
behind it never recovers.

Fix: **`update_frequency` 20 → 5** on all four (`warehouse.sdf` and `robofetch.gazebo.xacro`).
Measured rate fell to ~2.5 Hz wall-clock, the gripper answers parameter queries in ~2 s again, and a
full delivery completes. 5 Hz is ample for both the gripper's gap check and the dashboard map.

Geometry did **not** change, so the map does not need regenerating. If you add more per-model pose
publishers, remember they all land in the same executor — the budget is shared.

### 5.10 The grab check that could not fail (fixed)

`on_grab` used to measure the item-to-robot gap, publish attach, sleep, and measure again —
accepting the grab if the gap was still within tolerance. If the attach silently did nothing,
**neither body moved**, so the second measurement equalled the first and the check passed. Same
shape as the "3/3 delivered" bug in §5.4, one layer further down.

Fixed by subscribing to the DetachableJoint's `<output_topic>` (`/gripper/<item>/state`), which
publishes the literal strings `attached` / `detached`. Confirmed by reading them out of the plugin
binary, and live: the gripper now logs
`grabbed item_1 (joint reports 'attached', gap 0.50 m)`.

Two things to know if you touch this:
- **The topic fires only on TRANSITIONS**, so the subscription must exist before the start-up
  detach. It is created in the constructor, not lazily in `on_grab`, for exactly that reason.
  `ros2 topic echo ... --once` will hang on it — nothing is retained.
- **A proximity fallback remains** for the case where the joint has never reported, and it says so
  in the response message rather than pretending to be authoritative.

`on_release` verifies the same way: a detach that did not take hold would otherwise leave the parcel
welded to the gripper and dragged into the next delivery.

### 5.11b Deliveries landed 0.6-0.8 m from the bay — the error was made at GRAB time

**Symptom.** Everything reported success, but every parcel ended up well outside the bay marker.

**Cause — three, compounding.** None of them are at the release step, which is where it looks
like the fault must be:

1. `navigate_to` defaults to `yaw=0.0`, so the robot arrived at every pick point facing east,
   whatever it had come to collect.
2. `hold_tolerance` was 0.85 m with **no direction check**, so the gripper would weld a parcel
   sitting beside or behind the robot just as happily as one in front.
3. `DetachableJoint` holds the parcel at whatever offset it had when attached, and releases it
   at that same offset. So a parcel caught 0.8 m off to one side was carried 0.8 m off to one
   side and dropped 0.8 m from the bay. **The delivery inherited the grab's error.**

**Fix.**

- `TaskManager.approach(point, target, standoff)` drives to a point *facing* something. At the
  pick point it faces the shelf, so the parcel is dead ahead.
- The gripper refuses a grab outside `grab_half_angle_deg` (60 deg) of its heading, or closer
  than `min_grab_distance` (0.15 m, i.e. the approach overshot and the chassis is on top of it).
- The delivery approach stops `carry_offset` (0.55 m) **short** of the bay, facing it, so the
  parcel — which rides that far ahead — lands on the bay rather than past it.
- `delivery_tolerance` 1.1 -> 0.7, and `DELIVERY_TOLERANCE` in the acceptance suite with it.

**Measured after:** 0.31 m (shelf_3 -> delivery_1) and 0.24 m (shelf_2 -> delivery_2).

**Two things that did NOT work, so do not retry them:**

- *Calling `_snap_to_gripper` after a successful attach.* It teleports the parcel to a fixed
  point ahead of the robot, which sounds like the ideal fix and is why the helper exists. In
  practice it put parcel_5 **inside shelf_3**, Gazebo's physics ejected it across the room, and
  the delivery failed at 8.07 m. The helper remains deliberately unused.
- *Facing the parcel using `self.parcel_poses`.* `/model/<parcel>/pose` is **event-driven**: a
  parcel sitting still on a shelf never publishes, so the task manager's copy of that pose is
  simply absent when it matters. The shelf centre is sent with the order instead, because the
  database always knows it.

### 5.12 "I have no frame" — one leftover `route_server` breaks every later launch

**Symptom.** RViz shows no frames; `planner_server` logs `Timed out waiting for transform from
base_footprint to map`; AMCL repeats `cannot publish a pose`; the task manager sits at
`AMCL never confirmed the initial pose`; bringup ends with **"Failed to bring up all requested
nodes. Aborting bringup."** Intermittent, and it gets worse the more times you relaunch.

**Cause.** `scripts/stop.sh` kills Nav2 nodes by name, and its list was missing exactly one:
`route_server`, which is new in Nav2 Jazzy and postdates the line. So any unclean stop left one
alive. That orphan keeps its **node name** on the DDS network; the next launch starts its own
`route_server`; two nodes now answer to the same name; the new lifecycle manager's `change_state`
calls go astray and time out. No navigation stack means no `map` frame, so nothing localizes.

One survivor breaks every run after it, which is why it looks like a load problem — the orphans
accumulate across a session.

**How it was found.** `ps -eo pid,etimes,comm` showed a `route_server` aged 2667 s sitting beside
a run whose processes were all 145 s old. Elapsed time is the tell; the process list alone looks
normal.

**Fix.** `route_server` added to `PATTERN`, plus a self-audit:

```bash
./scripts/stop.sh --check      # names any live ROS process stop.sh would NOT kill
```

Run it while the system is up. Anything it lists is a future leftover. **If you add a Nav2
server, add it to `PATTERN`** — `--check` exists to catch the omission, but only if you run it.

### 5.13 `/clock` runs at ~500 Hz and every sim-time node pays for it

Not a bug, but the single largest CPU cost in the system, and worth knowing before blaming
anything else.

`warehouse.sdf` sets `max_step_size 0.001`, so Gazebo steps at 1 kHz and `/clock` is bridged at
~500-600 Hz (measured). **Every node with `use_sim_time: true` subscribes to `/clock`** — about
twenty of them — purely to know what time it is. In a Python node that costs far more per message
than the node's real work: measured while completely idle,

| node | CPU |
|---|---|
| `component_container` / Nav2 | ~206% |
| Gazebo (`ruby`) | ~174% |
| `gripper_node` | ~114% |
| `task_manager` | ~113% |
| `robot_state_node` | ~60% |

Measure it with `/proc/<pid>/stat` deltas, **not** `top`: `top -b`'s first iteration reports
lifetime averages, so mixing its output blocks gives numbers that are wrong by an order of
magnitude. That mistake was made once already during this investigation.

Three levers exist, **none tested, and one was reverted after a failed launch**:

- `use_sim_time: False` on `gripper_node` and `robot_state_node`. Both use `time.time()` and
  `time.sleep()` and read no ROS time at all, so they gain nothing from `/clock`. `task_manager`
  must keep it: it stamps the initial pose for AMCL, and a wall-clock stamp against a sim-time TF
  buffer is rejected as extrapolation, so the robot never localizes.
- `use_composition: True` in `navigation.launch.py` — Nav2's own default, which this project
  overrode. Collapses fifteen processes into one container. It **did** come up cleanly when
  tried, but only once, which is not enough evidence to keep.
- Raising `max_step_size` to `0.004` would cut `/clock` to ~125 Hz and help every node at once,
  but the parcel physics in §5.5 were tuned at 1 ms and a coarser step may change grab and drag
  behaviour. Needs a real delivery run to confirm.

**It is no longer only a GUI problem — on 2026-08-25 it blocked a HEADLESS launch.** Running
`./scripts/run.sh --headless` immediately after its own `colcon build`, the navigation stack never
came up. The tell is one line, and it is easy to read as the opposite of what it means:

```
[controller_server.rclcpp]: failed to send response to /controller_server/change_state
    (timeout): client will not receive response
```

`controller_server` **configured successfully** - `Configured MPPI Controller: FollowPath` is
right above it - but its reply to `lifecycle_manager`'s `change_state` call timed out in transit,
so the manager never learned it had succeeded and sat on `Configuring controller_server` for ever.
Downstream, AMCL repeated `cannot publish a pose` and the task manager stayed at
`waiting for Nav2 and gripper`, which looks exactly like the §5.12 orphan symptom - except
`stop.sh --check` was clean, and this had a different cause.

Measured at that moment, with everything idle: `ruby` 199%, `gripper_node` 123%, `task_manager`
120%, `parameter_bridge` 63%, `robot_state_node` 61%. About **570% consumed before Nav2 asked for
any**, on a machine whose load average was **13.00 on 12 cores** because `run.sh` had just built
the workspace.

**The fix needed no code.** Stop, wait for the load average to fall below ~2.5, and relaunch with
`--no-build`; it localized in about 40 seconds. So if a headless launch wedges at
`Configuring controller_server`, check `/proc/loadavg` before suspecting anything else - and
prefer building as a separate step from launching:

```bash
./scripts/stop.sh && colcon build --symlink-install     # build first, on its own
until [ "$(awk '{print ($1 < 2.5)}' /proc/loadavg)" = 1 ]; do sleep 10; done
./scripts/run.sh --headless --no-build                  # then launch onto a quiet machine
```

This is the strongest argument yet for actually testing the three levers listed above: the idle
`/clock` cost is what leaves no headroom for the lifecycle handshake.

None of this is required for the system to work: the acceptance suite passes 26/26 headless. It
matters for making the **GUI/RViz** run comfortable, and - as above - for launching reliably on a
busy machine.

### 5.14 A refused order is not a served order — the FR6 FIFO check counted it as one

Found on 2026-08-25, immediately after `CAPACITY_WH` dropped to 11. The acceptance suite reported

```
FAIL  FR6      orders are served oldest-first (FIFO)
      submitted [4, 5], started [5, 4] - the earlier order ran first
```

The message contradicts itself, which is the first clue that the **check** was wrong rather than
the scheduler. Ground truth from the API: order 4 `completed`, *"delivered (0.29 m from target)"*;
order 5 `refused`, *"predicted infeasible in the robot's current condition (confidence 54%)"*.

Order 5 **never entered the queue**, so there was never an ordering to violate. `check_fr6_fifo`
counted an order as started once its status was `!= "pending"`, and `refused` satisfies that. It
also polls the later order first on purpose, so the refusal landed at the head of `started` and
the comparison failed.

**Why the smaller battery exposed it.** The two orders are submitted 0.2 s apart. At 22 Wh the
second was affordable against the first one's reservation and sat at `pending` as intended. At
11 Wh it is refused instead - correct behaviour from admission control, and the reason the check
had never failed before.

**Fixed** by making the check say what actually happened:

- `NEVER_RAN = ("pending", "refused", "cancelled")` - a start means the order really began
  executing, not merely that it stopped being pending;
- a refusal now reports *"could not test FIFO: order(s) [5] were refused at admission ... - the
  queue never held two orders"*, which is an inconclusive setup rather than a scheduler fault;
- `wait_for_charge()` runs first so both orders are affordable and the check has a real queue;
- the evidence string no longer prints *"the earlier order ran first"* when it just failed.

**The general lesson**, and the reason this sits beside §5.4 and §5.10: a check that asks
"is this no longer in state X?" is not asking "did the thing I care about happen?". Both earlier
bugs had the same shape - a test that passes when nothing happened at all. This one was the
mirror image, failing when nothing had gone wrong.

### 5.11 v2 traps worth knowing

**`stop.sh` missed the SECOND web service.** v2 added `robofetch_ai` on port 8001, and the
pattern only matched the bridge, so the AI service survived every stop exactly the way the API
used to. The next launch's predictor then died with "address already in use" and every admission
decision silently fell back to the policy-only path - the system looked fine, just quietly
without its model. Found one orphan 37 minutes old. The pattern is now `uvicorn robofetch_`
rather than a list of names, so a service added later is covered without anyone remembering to
update it, and `run.sh` pre-flights **both** ports.

**`ps -o comm` truncates process names to 15 characters**, and `stop.sh` matches against that
truncated form. v2's condition monitor is `robot_state_node` -> `robot_state_nod`, which the
existing `robot_state_pub` entry (for `robot_state_publisher`) does NOT match, so it survived
every stop. Five accumulated across restarts, each still publishing `/robot/telemetry`, so the
web tier was reading battery and temperature from several stale robots at once - exactly the
kind of fault that looks like a broken sensor model rather than a leftover process. The entry is
now the prefix `robot_state`, which covers both.

**colcon does not delete files you removed from source.** Replacing the v1 dashboard left
`index.html`, `app.js` and `style.css` behind in `install/robofetch_web/share/.../web/` as
DANGLING symlinks pointing at files that no longer exist. Harmless here, but the general fix when
a package's file layout changes is `rm -rf build/<pkg> install/<pkg>` before rebuilding.

**The v1 dashboard URL was `/app`.** Anyone with that bookmarked got a 404 after the upgrade, and
a browser holding the old cached page at `/` showed the v1 UI as though nothing had changed.
`/app/*` now 308-redirects to `/`, and every page sends `Cache-Control: no-store` - which it
needs anyway, since each page is a snapshot of live state.

**`Jinja2Templates.TemplateResponse` changed signature.** Starlette >= 0.29 wants
`TemplateResponse(request, name, context)`. Passing the old `(name, context)` form makes it read
the template NAME as the request and the context dict as the name, and it dies deep inside the
template cache with `TypeError: unhashable type: 'dict'` — every page 500s while the stylesheet
still serves fine, which points suspicion in entirely the wrong direction.

**`python-multipart` is required** for the HTML form posts (`Form(...)`). Without it FastAPI
raises at request time, not at import, so the app starts happily and then fails on first use.

**The v1 database is not forward-compatible.** `CREATE TABLE IF NOT EXISTS` silently leaves an old
`orders` table in place with the wrong columns. Delete `robofetch.db` when moving to v2 — it is
runtime state and is recreated with the seeded catalogue on first run.

**Constants were retuned so the demo has something to refuse.** `CAPACITY_WH` is 11 (one heavy
delivery per charge; see §0) and `K_HEAT` is 1.2, which puts the lightest product's steady-state
temperature around 65 C and the heaviest around 74 C — straddling the 70 C limit. That is what
makes payload weight actually decide an outcome. A bigger battery or gentler heating is more
realistic but leaves the accept/refuse logic with nothing to bite on.

`CAPACITY_WH` is the one constant that reaches outside its own file: the ML training label
depends on what fraction of the pack an order consumes, so changing it silently invalidates
`model.joblib`. Regenerate and retrain whenever it moves:

```bash
./robofetch_venv/bin/python tools/ml/generate.py --runs 4000 --out tools/ml/runs.csv
./robofetch_venv/bin/python tools/ml/train.py --data tools/ml/runs.csv \
    --out src/robofetch_ai/robofetch_ai/models/model.joblib
```

### 5.9 "I submitted an order and nothing happened"

The single most confusing failure in the project, because **every** cause returns the same valid JSON
order to the client. Three independent ones, all fixed on 2026-08-05:

1. **A stale API holds port 8000.** The real launch's API dies with *"address already in use"* while
   `curl` keeps answering — from the old process, wired to a different database and no robot. See
   §5.1. `run.sh` now refuses to launch if the port is still held after `stop.sh`.

2. **The task manager became a zombie.** If AMCL did not confirm the initial pose within 90 s,
   `_run()` simply `return`ed. The node stayed alive and still *subscribed to `/orders/new`*, so the
   API accepted orders normally and every one sat at `pending` forever, with no error in the API, the
   database or the response. Worse, the cause is usually temporary — a loaded machine right after a
   `colcon build`. It now **retries localization indefinitely** and logs
   `Localized and ready - orders submitted now will execute.` when it can actually work.

3. **Publishing to a topic with no subscribers succeeds silently.** An order sent to a robot that is
   not running looked identical to an accepted one. `create_order` now checks
   `order_pub.get_subscription_count()` and, when nothing is listening, stores the order (the
   database is the record of what was *asked for*) with the detail
   `no robot is subscribed to /orders/new - order stored but NOT sent`.

`GET /health` now reports `robot_connected` and `orders_new_subscribers`, which distinguishes all
three in one request — a stale API answers with `robot_connected: false` and the wrong `database`.

---

## 6. Milestone status

These are the **v1 construction milestones**, kept as history. In the report they appear as the
construction stages inside **increment 1** (report §"The Increments"); the report's three
increments are v1 core delivery, v2 decision layer, v3 operational control. Two milestones were
later removed on purpose — a feature built, verified and then deleted is part of the project's
story, and §5 and §7 follow the same convention. For what the system does *now*, read §0.

| # | Milestone | State | Evidence |
|---|---|---|---|
| M1 | World + drivable robot | ✅ | `/scan`, `/odom`, `/tf`, `/cmd_vel` bridged; robot drives |
| M2 | Map + Nav2 navigation | ✅ | A→B→A both SUCCEEDED; AMCL error 0.05–0.14 m |
| M3 | Gripper + DetachableJoint | ✅ | grab/release/re-attach verified; refuses out-of-reach grabs |
| M4 | Task Manager, one order | ✅ | full navigate→grab→navigate→release |
| M5 | ~~Nearest-neighbour scheduler~~ | ⛔ superseded | Built and verified in v1 (submitted `[2,3,1]` → served `[1,3,2]`), then **removed**: v2 serves FIFO by order time via `Database.next_pending_order`, which is FR6 |
| M6 | ~~Grab-retry FSM~~ | ⛔ partly superseded | The state machine was removed; the behaviour it guarded survives as a plain 3-attempt grab then return to station, which is FR8 |
| M7 | FastAPI + SQLite + bridge | ✅ | HTTP → SQLite → ROS → robot → status back to SQLite |
| M8 | **Web dashboard** | ✅ | `localhost:8000` — order form, order table, analytics. The v1 live warehouse map is **gone**: v2 is server-rendered with no JavaScript at all (NFR3), and there is no `.js` file under `src/robofetch_web/` |
| M9 | **Bringup + tests + report** | ✅ | now **23 test cases + 26 acceptance checks** (was 63 + 10 in v1), README, INSTALL, UML diagrams |

---

## 7. Known limitations (be honest about these in the report)

> **RESOLVED — read §5.11b.** The two limitations immediately below were fixed after this section
> was written, and the record is kept because the *failed* attempts under each are exactly the
> "do not retry this" history the document exists for. Delivery accuracy is now **0.18-0.49 m**,
> and the grab check is authoritative.

**~~Delivery accuracy is ~0.7 m.~~** The dominant error was that `DetachableJoint` welds the parcel
wherever it lies at grab time (often ~0.5 m off-centre) and releases it at that same offset. Nav2's
0.25 m parking is secondary. **Fixed in §5.11b** by facing the parcel at grab time
(`TaskManager.approach`), refusing off-axis grabs, and stopping one `carry_offset` short of the
bay — the four failed attempts below predate that fix.

**Four attempts to improve it all FAILED and were reverted** (2026-08-04). Do not retry these
without changing what made them fail:

1. `xy_goal_tolerance` 0.25 → 0.12 and → 0.20 — MPPI at batch 600 cannot park that precisely, so the
   goal checker never passes and goals abort. **Leave at 0.25.**
2. Snapping the parcel to the gripper via `gz service .../set_pose` before attaching. The service
   works and the robot still drives, but full runs stopped completing. Dead code remains as
   `gripper_node._snap_to_gripper`, no longer called.
3. Aiming the drop-off goal one carry-offset short so the *parcel* lands on target. With
   `approach_yaw=0` the goal fell inside the west wall's inflation; `approach_yaw=pi` fixed the
   geometry but runs still failed.
4. `carry_offset` 0.40 → 0.55 — no improvement.

**The two plausible real fixes** (neither attempted): an **approach-and-align step before grabbing**
so the parcel is always taken centred; or **run headless with full-strength MPPI** so tighter parking
becomes achievable.

**~~The grab check cannot tell "attached" from "nothing happened"~~** (found 2026-08-05,
**fixed in §5.10** by subscribing to the DetachableJoint's `<output_topic>`, exactly as the last
paragraph of this entry proposed). `gripper_node.
on_grab` measures the item-to-robot gap, publishes attach, sleeps, and measures the gap again —
accepting the grab if it is still within `hold_tolerance` (0.85 m). If the attach silently does
nothing, neither body moves, so `gap_after == gap_before` and the check **passes trivially**. This is
the exact shape of the bug §5.4 was written about, one level further down. Observed live: the gripper
logged *"grabbed item_1 (gap 0.60 m)"*, the robot drove to the drop-off, and the parcel never left
the shelf. Only the task manager's end-of-order pose check caught it — which it did, correctly,
reporting *"item_1 ended 3.64 m from the drop-off - it was never carried"*. No false success reached
the database.

The authoritative signal already exists and is unused: the DetachableJoint's `<output_topic>`
(`/gripper/<item>/state`) is declared in the URDF and bridged in `bridge.yaml`, whose own comment
calls it *"the authoritative answer to 'is the parcel actually held?', far better than guessing from
proximity"*. It publishes only on transitions, so a subscriber must be alive before the attach — a
node-lifetime subscription that caches the last state, rather than a one-shot read.

A second fragility in the same method: `_gap_to_gripper` compares the **AMCL** robot position against
the **Gazebo** item position. Those agree only while localization is good, so a drifting AMCL (§5.5)
silently corrupts the grab decision too.

**Cancelling an order does not stop the robot** (pre-existing; confirmed 2026-08-05 now that the
dashboard puts a Cancel button in front of you). `DELETE /orders/{id}` only writes `cancelled` to
SQLite. The order was already published to `/orders/new` the moment it was created, so the task
manager has it queued and executes it regardless — and its own status updates then overwrite
`cancelled` with `navigating`, `delivering`, `completed`. Observed exactly that: order 11 read
`cancelled` at T+0.1 s and `delivering` at T+24 s.

`db.cancel_order` refuses anything not still `pending`, which sounds like a guard but is not one:
orders reach the robot within milliseconds, so the window where cancelling is *meaningful* is
essentially zero. A real fix needs a cancel path to the robot — a `/orders/cancel` topic the task
manager honours, dropping the order if queued and calling `nav_client.cancel_goal` if it is running.
Until then, treat the button as "cancel if it truly has not started yet".

**Reliability is genuinely variable, and worth being honest about in the report.** On 2026-08-05,
back to back on the same build: one run delivered **0/2** (one silent grab failure, one corridor
trap) and the next delivered **2/2**, both verified against Gazebo at 0.66 m and 0.59 m. The
difference was not code. The failing run was on a machine already loaded to ~18 on 12 cores, which is
the same CPU-starvation sensitivity §5.5 documents for MPPI. Always check physical parcel positions
rather than assuming — `./scripts/order.sh` now does that automatically at the end of every order.

---

## 8. Next steps

**M8 is done.** `robofetch_web` is an ament_cmake package installing `web/` to its share directory;
`delivery.launch.py` points `ROBOFETCH_WEB` at it via `additional_env`, which is what activates the
mount in `app.py` (without that the mount is dead code and there is no UI). Verified 2026-08-05:
`/`, `/app/`, `/app/style.css`, `/app/app.js` all 200, and a full delivery produced 8 order events,
75 pose updates and **50 parcel-position updates** on the WebSocket — the map animates the parcel
from shelf to station.

**M9 — Bringup, tests, report:** formalise integration and acceptance tests, write the README, and
assemble the report with the UML diagrams.

Two things worth doing before or alongside M9:
- **The `shelf_1`/`shelf_2` corridor trap (§5.5)** is still present and is the main remaining cause
  of failed orders.
- **The grab check still cannot detect a failed attach (§7)** — `/gripper/<item>/state` is bridged
  and unused.

---

## 9. Working agreements

- **End every iteration with "how to try it yourself"** — exact commands, expected output, and what
  failure looks like. The owner runs everything on their own machine.
- **Verify against Gazebo ground truth**, never logs/RViz alone.
- **`./scripts/stop.sh` before every relaunch.**
- **Test with the GUI running** — headless passes when GUI runs fail (different CPU load).
- Keep `scripts/generate_map.py`'s `WALLS` in sync with `warehouse.sdf`; re-run it and rebuild after
  any world change.
- The owner is learning: explain *why*, not just *what*.
