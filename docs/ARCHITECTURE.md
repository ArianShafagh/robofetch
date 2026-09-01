# RoboFetch — Architecture, package by package

What every package is, why it exists as its own package, what is inside it, and how it relates to
the rest. The last section rebuilds the whole thing from an empty folder, in the order the
dependencies force.

Read `docs/proposal.md` for *what* the system does. This file is about *how it is arranged*.

---

## 1. The shape of the whole thing

Nine packages. The arrows are "depends on", and they only ever point one way — there are no cycles,
which is what lets any layer be tested without the ones above it.

```
robofetch_interfaces          (message shapes: no code, no dependencies)
        ▲
robofetch_core                (OUR robot logic: model, task manager, gripper)
        ▲                ▲
robofetch_bridge         robofetch_bringup ──▶ robofetch_gazebo ──▶ robofetch_description
   (web + database)             │                     ▲
        ▲                       └──────────▶ robofetch_nav
robofetch_web                                          
   (templates)                robofetch_ai   (separate process, depends on nothing)
```

Two facts about this graph are the whole design:

**`robofetch_core` depends on nothing above it.** It does not know the web exists. The physics
model inside it does not even know ROS exists. That is why its tests run in milliseconds with no
simulator.

**`robofetch_ai` depends on nothing at all.** It is deliberately reachable only over HTTP, so that
"what happens when the AI is down" is a question you can answer by stopping a process rather than
by reading code.

### Build systems

| Type | Packages | Why |
|---|---|---|
| `ament_python` | `core`, `bridge`, `ai` | They contain Python that runs as nodes or services |
| `ament_cmake` | `interfaces`, `description`, `gazebo`, `nav`, `web`, `bringup` | They only install files — worlds, URDFs, params, templates, launch |

A package that only ships data does not need a Python build. `robofetch_web` is `ament_cmake`
because it installs HTML; it contains no code at all.

---

## 2. `robofetch_interfaces` — the vocabulary

```
srv/Grab.srv
CMakeLists.txt
package.xml
```

One file of substance:

```
string item          # model name of the item, e.g. "item_1"
bool release false   # false = grab, true = release
---
bool success         # did the operation verifiably take effect?
string message       # human-readable detail
float64 distance     # metres between gripper and item at verification time
```

**Why it is its own package.** ROS generates code from `.srv` files, and anything that uses the
service needs that generated code. Keeping the definition alone in a package with no dependencies
means both sides — the caller and the gripper — depend on the *shape of the message* and not on
each other.

**Why the response has three fields and not one boolean.** `success` alone would repeat the
project's original mistake: a command reporting on itself. `message` carries the evidence
("joint reports 'attached'" versus "the joint has not reported, so this is unverified") and
`distance` carries the measurement. A caller can therefore tell a verified success from an
assumed one.

---

## 3. `robofetch_description` — what the robot is

```
urdf/robofetch.urdf.xacro      the body: links, joints, inertias, the lidar and gripper mounts
urdf/robofetch.gazebo.xacro    the simulator plugins attached to that body
launch/rsp.launch.py           publishes the description so everything else can read it
```

**Why URDF is split into two files.** The first describes a robot; the second describes how *this
simulator* animates it. Keeping them apart means the body could be driven by different plugins, or
published to RViz with no simulator at all, which is exactly what `rsp.launch.py` does.

The Gazebo file is where the differential drive lives:

```xml
<plugin filename="gz-sim-diff-drive-system">
  <left_joint>left_wheel_joint</left_joint>
  <right_joint>right_wheel_joint</right_joint>
  <wheel_separation>0.26</wheel_separation>
  <wheel_radius>0.06</wheel_radius>
  <max_linear_acceleration>1.0</max_linear_acceleration>
</plugin>
```

That plugin is what turns a velocity command into wheel rotation. It is the reason `/cmd_vel`
means anything.

**A trap recorded in the file itself:** URDF-to-SDF conversion merges links joined by fixed joints
into their parent, which made `gripper_link` disappear and broke the detachable joint. The fix is
`<preserveFixedJoint>` and `<disableFixedJointLumping>` on that joint.

---

## 4. `robofetch_gazebo` — the world

```
worlds/warehouse.sdf     the room, three shelves, six parcels, the detachable joints
config/bridge.yaml       which topics cross between ROS and Gazebo, and in which direction
launch/sim.launch.py     starts the simulator and the bridge
```

**`bridge.yaml` is the most important file here**, because ROS and Gazebo are two separate message
systems that share nothing. Every topic that crosses must be declared:

```yaml
- ros_topic_name: "cmd_vel"
  gz_topic_name: "cmd_vel"
  ros_type_name: "geometry_msgs/msg/Twist"
  gz_type_name: "gz.msgs.Twist"
  direction: ROS_TO_GZ
```

Direction matters. `cmd_vel` goes ROS→Gazebo (commands in); `odom` and `scan` go Gazebo→ROS
(sensing out). Forget a line and the topic silently does not exist — which looks like a broken
node rather than a missing declaration.

**Why the warehouse is 8 × 6 m and not square.** A square room is rotationally symmetric, so a
laser scan taken at 0° and at 90° are identical and the localizer genuinely cannot tell the poses
apart. The room is rectangular and the three shelves are different sizes at different orientations
specifically so that every part of the map produces a distinctive scan.

**Why the shelves are flush against the walls.** A gap narrower than about 1.2 m is a trap: a
0.44 m robot with 0.35 m costmap inflation cannot fit, drives in, and wedges. `generate_map.py`
carries the same three rectangles and a comment saying so — **the two must be kept in sync, or the
map disagrees with the world.**

---

## 5. `robofetch_nav` — how it moves

```
config/nav2_params.yaml    every navigation parameter
maps/warehouse.yaml        the occupancy grid, generated from the world geometry
launch/navigation.launch.py
```

This package contains **no code of ours**. Nav2 is a third-party stack (category B1); this package
configures it. The engineering here is in the parameters, and two of them are load-bearing:

```yaml
controller_frequency: 10.0    # MPPI publishes /cmd_vel this often
model_dt: 0.1                 # must equal 1/controller_frequency
```

Those two must change together — MPPI refuses to configure if the period exceeds `model_dt`, and a
controller that will not configure aborts the entire bringup.

**Why the map is generated rather than built by SLAM.** The proposal specifies a known map; SLAM
maps drifted. `scripts/generate_map.py` draws the occupancy grid analytically from the same
rectangles that define the world, so the map frame and the world frame are identical and every
waypoint can be written as a literal world coordinate.

---

## 6. `robofetch_core` — the project's own robot logic

The most important package. Everything here was written for this project.

```
robofetch_core/robot_model.py       the physics: pure functions, no ROS, no web, no database
robofetch_core/robot_state_node.py  integrates that model against what the robot really does
robofetch_core/task_manager.py      the FIFO queue and the delivery sequence
robofetch_core/gripper_node.py      attach and detach, with verification
```

### `robot_model.py` — 198 lines, and it imports one thing

```python
from dataclasses import dataclass, asdict
```

That is the entire import list. No FastAPI, no SQLite, no `rclpy`. It is the energy formula, the
thermal and wear dynamics, the route arithmetic and the thresholds:

```python
E_BASE = 0.35            # Wh per metre, unloaded
E_LOAD = 0.08            # Wh per metre per kg
CAPACITY_WH = 11.0       # one heavy delivery per charge
RESERVE_PERCENT = 15.0   # must remain AFTER returning to the station
T_MAX = 70.0
CONDITION_MIN = 30.0
```

**Why this file has no dependencies at all.** Four different things need these equations: the
condition monitor, the gripper, the admission workflow in the web tier, and the unit tests. If the
model lived inside any one of them, the others would need a copy — and the robot's real battery
drain would be computed by different code than the estimate shown to the operator. They would
drift, silently.

It is also what makes `test_robot_model.py` run in milliseconds with no simulator.

### `task_manager.py` — 748 lines, the largest file we wrote

It owns the queue and executes one order at a time:

```
navigate to the pick point (facing the shelf)  →  grab  →  navigate to the bay
(stopping one carry-offset short)  →  release  →  verify against the simulator
```

Three structural details worth knowing:

- **A worker thread and a callback thread.** The worker runs the queue and spends most of its life
  blocked on a navigation result. The emergency stop arrives on a subscription callback, which is
  the only reason it can act while the worker is blocked.
- **`_abort` is a `threading.Event`, not a database column.** The stop is an action that completes,
  not a mode the system sits in, so nothing about it survives a logout.
- **`_stuck_parcel`** blocks all further work when a release could not be verified, because
  starting the next order would drag the old parcel to the next shelf and corrupt every delivery
  after it.

### `gripper_node.py` — 418 lines

Serves `~/grab` and `~/release`. The important thing is what counts as evidence: it subscribes to
the detachable joint's own state topic, which publishes the literal strings `attached` and
`detached`, and treats that as the authority rather than the fact that a command was sent.

**The subscription is created when the node starts, not when a grab is requested** — that topic
carries a message only on a *transition*, so a subscriber created at the moment of the grab would
miss the very event it exists to observe.

### `robot_state_node.py` — 193 lines

Integrates the condition model at 1 Hz against real odometry, publishes `/robot/telemetry`, and
writes a per-run CSV. Distance comes from `/odom` rather than from the commanded velocity on
purpose: if the robot is stuck against an obstacle, the wheels turn but the robot does not move,
and only odometry knows the difference.

---

## 7. `robofetch_bridge` — the web tier and the database

```
robofetch_bridge/db.py          808 lines  the SQLite layer: schema and every query
robofetch_bridge/app.py         660 lines  FastAPI: routes, pages, sessions
robofetch_bridge/admission.py   254 lines  the decision workflow (C2)
robofetch_bridge/ros_link.py    142 lines  the bridge between the web process and ROS
robofetch_bridge/predictor.py    59 lines  HTTP client for the AI service
```

This is the package that most often gets misread as "the web doing everything", so the internal
layering matters:

| File | Knows about |
|---|---|
| `app.py` | HTTP and templates. **Zero** formulas — grep it for `E_BASE` or `RESERVE_PERCENT` and you get nothing |
| `admission.py` | The physics model. **Zero** mentions of HTTP or SQL |
| `db.py` | SQLite only |
| `robot_model.py` (imported from `core`) | Nothing |

`app.py`'s preview endpoint is three lines: resolve, delegate, format. All of the decision lives in
`admission.decide()`, and all of the arithmetic lives one package below in `robot_model`.

### `db.py` — why one class owns the schema

Forty-two public methods over eleven tables. Two decisions shape it:

**An order references a product, never a coordinate.** The client sends `SKU-1001` and
`delivery_1`; coordinates are resolved at admission time. The warehouse can therefore be
rearranged by editing rows.

**The reservation ledger is derived, not stored.** `committed_load()` sums energy over orders in
non-terminal states rather than keeping a separate balance. A terminal status releases the
reservation automatically, so the ledger and the orders table cannot drift apart.

### `ros_link.py` — two runtimes in one process

FastAPI is `asyncio`; `rclpy` wants its own executor. `RosThread` runs the ROS node on a background
thread and exposes plain method calls to the web code. It is also where a real failure mode is
handled: publishing to a topic with no subscribers succeeds silently, so `publish_estop` returns
the subscriber count and `create_order` checks it — an order sent to a robot that is not running
would otherwise look exactly like one that worked.

---

## 8. `robofetch_ai` — deliberately a separate process

```
robofetch_ai/service.py      112 lines
models/model.joblib          the trained classifier
```

One endpoint:

```
POST /predict {battery_percent, temperature_c, condition_percent,
               payload_kg, route_distance_m}
  -> {feasible: bool, confidence: float, model_loaded: bool}
```

**Why it is a separate process on its own port**, rather than an import: so that "the AI is
unavailable" is a state you can produce by stopping a process. NFR2 requires the system to keep
working without it, and a network boundary makes that failure mode testable rather than
theoretical.

**What it predicts**: feasibility, not cost. It never sees an order, a product or a route — only
five numbers assembled by `admission.features()`. The energy estimate is computed deterministically
by `robot_model`; the classifier is gate 5 of five, and it can only ever *refuse* an order the
physical checks already allowed.

**Honest limitation:** the model is trained on synthetic data whose labels come from the same
thresholds the gates apply, so it does not know anything the formula does not. What it contributes
is a confidence on marginal orders, and a demonstration of graceful degradation.

---

## 9. `robofetch_web` — templates only

```
web/templates/*.html    eleven Jinja2 templates
web/static/style.css    about 130 lines
CMakeLists.txt          installs them; there is no Python here
```

**Why templates are a separate package from the code that renders them.** `delivery.launch.py`
points the environment variable `ROBOFETCH_WEB` at this package's share directory, so the web tier
finds its templates through the ROS install tree like any other resource. It also means the UI can
be replaced without touching the application.

Two details that cost real time to learn:

- **`StaticFiles` refuses to serve symlinks**, and `colcon build --symlink-install` creates them.
  The fix is `follow_symlink=True`. The failure is deceptive: `/` still worked while every asset
  returned 404, so it looked like a frontend bug.
- **The stylesheet is cache-busted by its own mtime** (`style.css?v=<mtime>`), because a browser
  serving the previous stylesheet makes a redesign look like it never deployed.

---

## 10. `robofetch_bringup` — one command starts everything

```
launch/delivery.launch.py    120 lines
```

Starts eight things in the right order: the simulator, the bridge, the navigation stack, the three
robot nodes, the web tier and the prediction service.

Launch-file traps recorded here:

- **`IncludeLaunchDescription` leaks its arguments into the parent scope.** Passing `rviz:=false`
  to the sim include silently switched off the parent's RViz too. Wrap includes in
  `GroupAction(scoped=True)`.
- **ROS cannot infer the type of an empty array parameter**, so pre-queued orders are passed as a
  semicolon-separated string rather than a list.
- **Nav2 nodes are lifecycle nodes.** Launched raw they sit unconfigured, declare no parameters and
  publish nothing — alive but inert. Use their own launch files, which emit the transitions.

---

## 11. Outside the build

```
scripts/            run.sh, stop.sh, order.sh, acceptance.py, generate_map.py
tools/ml/           generate.py, train.py    — disposable, deleted once model.joblib exists
tools/report/       make_figures.py, make_uml.py, make_results_table.py
```

`tools/ml` is **deliberately outside the build** and nothing under `src/` imports it (NFR6). The
running system loads only the exported `model.joblib`.

---

## 12. Building it from zero

The dependency graph dictates the order. Each stage ends in something you can *see* working, which
is the whole reason the project was built incrementally.

**1 — A robot that exists.** `robofetch_interfaces`, then `robofetch_description`. Write the URDF,
add the Gazebo plugins, launch `rsp.launch.py` and confirm the robot appears in RViz. Nothing else
can be tested until a robot exists.

**2 — A robot that moves.** `robofetch_gazebo`. Write the world, declare the bridge topics, and
drive it with `teleop`. If `/cmd_vel` does nothing, the bridge declaration is missing — check
`bridge.yaml` before anything else.

**3 — A robot that navigates.** `robofetch_nav`. Generate the map from the world geometry, tune
`nav2_params.yaml`, and send a goal. Confirm localization error stays small and the robot reaches
two opposite corners.

**4 — A robot that manipulates.** `gripper_node` in `robofetch_core`. Subscribe to the joint's own
state topic *in the constructor*. Verify a grab and a release actually took hold.

**5 — A robot that does a job.** `task_manager`. Chain navigate, grab, navigate, release — then add
the verification step that reads the parcel's real pose and fails the order if it disagrees. Add
that step early: without it you will believe deliveries that never happened.

**6 — Somewhere to keep the facts.** `db.py`. Schema first, seeded with the catalogue and the
layout. Orders reference products, never coordinates.

**7 — A way in.** `app.py` and `ros_link.py`. HTTP to database to ROS and back. Check the
subscriber count when publishing, or an order sent to a robot that is not running will look
identical to one that worked.

**8 — A robot that knows itself.** `robot_model.py` and `robot_state_node.py`. Write the model with
no imports beyond `dataclasses`, so everything can share it.

**9 — A robot that can say no.** `admission.py`, then `robofetch_ai`. Gates first, model last, and
build the fallback path before the model — otherwise you will not notice that you depend on it.

**10 — One command.** `robofetch_bringup`, and `stop.sh` on the same day. A stop script that misses
one process will cost you hours later, in ways that look like anything except a leftover process.

### If you build it again, do these earlier

- **The verification step before the happy path.** Reported success and physical reality diverge
  silently, and the longer you wait the more results you have to distrust.
- **`stop.sh --check` on day one.** Every leftover process this project produced was found long
  after it started causing damage.
- **Acceptance criteria alongside each requirement.** Writing "how would I measure this?" next to
  NFR4 revealed that it was never met at all.
