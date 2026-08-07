# RoboFetch — Project Handover

Complete state of the project: what exists, how to run it, every problem hit and how it was
solved, and what remains. Written so a fresh session (or a new person) can pick it up cold.

**Last updated:** 2026-08-07 · **Status:** v2 implemented · **65 tests + 15 acceptance checks passing**

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
| NFR1 (<2 s preview) | measured **368 ms** — acceptance check |
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
**65 passing:** condition model, admission policy, database, and HTTP↔SQLite↔ROS integration.

Acceptance, against the live simulator (needs a freshly launched stack):

```bash
./robofetch_venv/bin/python scripts/acceptance.py          # --quick skips the slow physical ones
```
**15/15 passing** as of 2026-08-07.

### ⚠️ Verify with Gazebo, never with logs alone

```bash
source /opt/ros/jazzy/setup.bash
gz topic -e -t /world/warehouse/dynamic_pose/info -n 1 | grep -A6 'name: "item_'
```
This matters — see §5.4. The same command with `name: "robofetch"` gives the robot's true pose, which
is the only way to catch AMCL drifting (see §5.5).

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

**The same trap still exists behind `shelf_1` and `shelf_2`** (found 2026-08-05 — the rule above was
only ever applied to `shelf_3`). Both shelves end at y = 2.05 while the north wall's inner face is at
y = 2.95, leaving a **0.90 m corridor** across the whole north side, reachable through the 2.25 m gap
between the two shelves. Observed live: the robot drove in, wedged itself, and AMCL's belief diverged
from Gazebo ground truth by **2.85 m** — it reported (−2.03, −0.35) while the robot was really at
(−1.60, 2.50), inside that corridor. Because the scan was then taken from behind a shelf while the
costmap was written around the *believed* pose, the robot appeared **enclosed**: the global planner
failed with *"Failed to create plan with tolerance of 0.500000"* for **every** goal, including the
open centre of the room. Costmap clears and the wait-recovery did not help.

This is very likely the real identity of the "occasional goal abort" in §7. Fix: extend `shelf_1` and
`shelf_2` north so they are flush with the north wall, exactly as `shelf_3` is flush with the east
wall — then re-run `scripts/generate_map.py` and rebuild.

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

**Constants were retuned so the demo has something to refuse.** `CAPACITY_WH` is 40 (about 4–6
deliveries per charge) and `K_HEAT` is 1.2, which puts the lightest product's steady-state
temperature around 65 C and the heaviest around 74 C — straddling the 70 C limit. That is what
makes payload weight actually decide an outcome. A bigger battery or gentler heating is more
realistic but leaves the accept/refuse logic with nothing to bite on.

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

| # | Milestone | State | Evidence |
|---|---|---|---|
| M1 | World + drivable robot | ✅ | `/scan`, `/odom`, `/tf`, `/cmd_vel` bridged; robot drives |
| M2 | Map + Nav2 navigation | ✅ | A→B→A both SUCCEEDED; AMCL error 0.05–0.14 m |
| M3 | Gripper + DetachableJoint | ✅ | grab/release/re-attach verified; refuses out-of-reach grabs |
| M4 | Task Manager, one order | ✅ | full navigate→grab→navigate→release |
| M5 | **Nearest-neighbour scheduler** | ✅ | submitted `[2,3,1]` → served `[1,3,2]`; NFR2 = 0.0136 ms |
| M6 | **Grab-retry FSM** | ✅ | 3 attempts w/ backoff → order failed → queue continued |
| M7 | FastAPI + SQLite + bridge | ✅ | HTTP → SQLite → ROS → robot → status back to SQLite |
| M8 | **Web dashboard** | ✅ | `localhost:8000` — order form, live table, warehouse map, analytics; verified end to end |
| M9 | **Bringup + tests + report** | ✅ | 63 pytest + 10 acceptance checks, README, UML diagrams |

---

## 7. Known limitations (be honest about these in the report)

**Delivery accuracy is ~0.7 m.** The dominant error is that `DetachableJoint` welds the parcel
wherever it lies at grab time (often ~0.5 m off-centre) and releases it at that same offset. Nav2's
0.25 m parking is secondary.

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

**The grab check cannot tell "attached" from "nothing happened"** (found 2026-08-05). `gripper_node.
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
