"""Task Manager - executes a delivery order end to end (proposal UC4, FR6-FR9, FR11).

    navigate to the pick point  ->  grab  ->  navigate to the delivery bay  ->  release

Navigation is delegated to Nav2's NavigateToPose action (third-party, used as-is) and grabbing
to the gripper node's services. The orchestration, state tracking and error handling are ours.

Three deliberate simplifications compared with v1:

  * **Orders are served FIFO.** v1 picked the geographically nearest pending pickup. Customers
    care about being served in the order they asked, not about the robot's convenience, so the
    web tier hands orders over one at a time in `created_at` order and this node simply runs
    what it is given.
  * **A failed grab is retried 3 times, then the order fails** and the robot drives home. v1
    had a state machine with exponential backoff; it was more machinery than the behaviour
    justified.
  * **After 2 minutes idle the robot returns to its station** and reports `charging`, which is
    what lets the condition model recover battery and temperature.

The sequence runs on a worker thread and blocks on futures while a MultiThreadedExecutor
services callbacks - much easier to follow than a callback-chained state machine.
"""
import json
import math
import threading
import time

import rclpy
from geometry_msgs.msg import Pose, PoseWithCovarianceStamped, Twist
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

from robofetch_interfaces.srv import Grab

MAX_GRAB_ATTEMPTS = 3
# The gripper node already retries the detach internally; these are whole service calls on top
# of that, for the case where the gripper itself is wedged or slow to answer.
MAX_RELEASE_ATTEMPTS = 2

# The kinds of task this node serves. Kept as plain strings matching robofetch_bridge.db so the
# two sides agree without the robot having to import the web tier.
KIND_DELIVERY = "delivery"
KIND_RETURN = "return"


def yaw_to_quat(yaw):
    """Minimal yaw -> quaternion (z, w); roll and pitch are always zero here."""
    return math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class TaskManager(Node):
    def __init__(self):
        super().__init__("task_manager")

        # The robot spawns on its station, which is also where it returns when idle.
        self.declare_parameter("publish_initial_pose", True)
        self.declare_parameter("initial_pose", [0.0, -2.2, 0.0])     # x, y, yaw
        self.declare_parameter("station", [0.0, -2.2])
        # How close the parcel must end up to count as delivered. Was 1.1 m, because the
        # DetachableJoint welded the parcel wherever it happened to lie and released it at that
        # same offset. The gripper now snaps a grabbed parcel to a fixed point ahead of the
        # robot, and the robot stops `carry_offset` short of the bay so the parcel lands on it,
        # which leaves only Nav2's own goal tolerance - so this can be honest again.
        self.declare_parameter("delivery_tolerance", 0.7)
        # Must MATCH gripper_node's `carry_offset`: it is where a carried parcel rides relative
        # to the robot centre, and the delivery approach stops exactly that far short of the bay.
        self.declare_parameter("carry_offset", 0.55)
        # Seconds with an empty queue before the robot drives home (FR11).
        self.declare_parameter("idle_return_seconds", 120.0)

        self.station = tuple(float(v) for v in self.get_parameter("station").value)
        self.delivery_tolerance = self.get_parameter("delivery_tolerance").value
        self.carry_offset = float(self.get_parameter("carry_offset").value)
        self.idle_return_seconds = self.get_parameter("idle_return_seconds").value

        start = self.get_parameter("initial_pose").value
        self.robot_position = (float(start[0]), float(start[1]))

        group = ReentrantCallbackGroup()
        self.nav_client = ActionClient(self, NavigateToPose, "navigate_to_pose",
                                       callback_group=group)
        self.grab_client = self.create_client(Grab, "/gripper_node/grab",
                                              callback_group=group)
        self.release_client = self.create_client(Grab, "/gripper_node/release",
                                                 callback_group=group)
        self.initial_pose_pub = self.create_publisher(
            PoseWithCovarianceStamped, "/initialpose", 10)

        # AMCL only publishes once it is active and has a pose estimate, which makes it a
        # reliable "the navigation stack is really ready" signal. The action server appearing
        # is not enough: Nav2's lifecycle nodes reject goals until activated.
        self._amcl_ready = threading.Event()
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose",
                                 self._on_amcl_pose, 10)

        # Parcel world poses, used to CONFIRM a delivery physically happened (C3). Reporting
        # success because the commands returned OK is not enough: a grab that silently failed
        # leaves the parcel behind while every step still reports success.
        self.parcel_poses = {}

        # --- link to the web bridge ---
        self.status_pub = self.create_publisher(String, "/orders/status", 10)
        self.activity_pub = self.create_publisher(String, "/robot/activity", 10)
        self.create_subscription(String, "/orders/new", self._on_new_order, 10)

        # --- emergency stop ---
        # Handled on the SUBSCRIPTION callback rather than the worker thread, because the worker
        # is usually blocked waiting on a navigation result when the button is pressed. That is
        # the whole point: the stop has to interrupt the wait, not queue behind it.
        self.create_subscription(String, "/robot/estop", self._on_estop, 10)
        # Zero velocity straight to the drive. Cancelling the Nav2 goal is what really stops the
        # robot, but the cancel takes a moment to propagate through the controller, and "stop"
        # should mean stop - so we also brake directly.
        self.halt_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        # Raised by the stop button, cleared by the worker once it has unwound. Transient
        # by design: this is an action that completes, not a mode the system sits in.
        self._abort = threading.Event()
        # The goal currently in flight, so it can be cancelled from another thread.
        self._nav_handle = None
        self._nav_lock = threading.Lock()
        # What the gripper is holding: the simulator model, and its mass. Reported to the
        # condition model when the robot is halted, so a stop while loaded does not pretend the
        # payload vanished - and used to put the parcel down before the robot is moved again.
        self._carrying_kg = 0.0
        self._carrying_model = None

        self._queue = []                     # FIFO, oldest first
        self._queue_lock = threading.Lock()
        self._seen_ids = set()

        # Set to a model name when a release failed and the parcel is believed to be still on
        # the gripper. While this is set the robot must not start another order: it would drag
        # the old parcel to the next shelf, and every delivery after that is wrong.
        self._stuck_parcel = None

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ incoming
    def _on_new_order(self, msg):
        """Queue a task sent by the web tier.

        Delivery: {"id", "kind": "delivery", "product_id", "model", "weight_kg",
                   "pickup": [x, y], "dropoff": [x, y]}
        Return:   {"id", "kind": "return", "dropoff": [x, y]}

        The bridge resolves products to coordinates, so this side stays numeric. `kind` defaults
        to delivery so an older bridge that does not send it still works.
        """
        try:
            order = json.loads(msg.data)
            order["kind"] = order.get("kind", KIND_DELIVERY)
            order["dropoff"] = tuple(order["dropoff"])
            order["id"] = int(order["id"])
            if order["kind"] == KIND_DELIVERY:
                order["pickup"] = tuple(order["pickup"])
                shelf = order.get("shelf") or [None, None]
                order["shelf"] = (tuple(shelf) if None not in shelf else None)
        except (ValueError, KeyError, TypeError) as exc:
            self.get_logger().error(f"Ignoring malformed order '{msg.data}': {exc}")
            return

        with self._queue_lock:
            if order["id"] in self._seen_ids:
                return                                   # duplicate; ignore
            self._seen_ids.add(order["id"])
            self._queue.append(order)                    # FIFO: append, pop from the front

        if order["kind"] == KIND_RETURN:
            self.get_logger().info(
                f"Accepted return-to-station task {order['id']}: -> {order['dropoff']}")
        else:
            self._watch_parcel(order["model"])
            self.get_logger().info(
                f"Accepted order {order['id']}: {order['product_id']} ({order['model']}, "
                f"{order['weight_kg']} kg) {order['pickup']} -> {order['dropoff']}")
        self._publish_status(order["id"], "pending", 0, "queued")

    def _watch_parcel(self, model):
        if model in self.parcel_poses:
            return
        self.parcel_poses[model] = None
        self.create_subscription(
            Pose, f"/model/{model}/pose",
            lambda msg, n=model: self.parcel_poses.__setitem__(
                n, (msg.position.x, msg.position.y)),
            10)

    def _on_amcl_pose(self, msg):
        self._amcl_ready.set()
        p = msg.pose.pose.position
        self.robot_position = (p.x, p.y)

    # ----------------------------------------------------------------- emergency stop
    def _on_estop(self, msg):
        """Abandon everything and stop. One shot - there is nothing to switch off afterwards.

        This does NOT latch. The flag it raises lives only long enough for the worker thread to
        notice, unwind the task it was running, and come back to an empty queue; `_serve_queue`
        clears it there. From the operator's side it is a single action with an end: press it,
        the robot drops the work and stands still, and that is the whole of it. Nothing to
        remember, nothing left engaged, and nothing to survive a logout.
        """
        if self._abort.is_set():
            return                                   # already stopping; let it finish
        self._abort.set()
        self.get_logger().error("EMERGENCY STOP - abandoning all work and halting.")
        self._halt()

    def _halt(self):
        """Stop moving now: cancel the goal, brake, and drop everything that was queued."""
        with self._nav_lock:
            handle = self._nav_handle
        if handle is not None:
            try:
                handle.cancel_goal_async()
            except Exception as exc:                                   # noqa: BLE001
                self.get_logger().warn(f"Could not cancel the navigation goal: {exc}")

        # Brake for a moment. One zero Twist can be missed if the controller publishes straight
        # after it, so hold the brake on until the cancel has certainly taken effect.
        stop = Twist()
        for _ in range(10):
            self.halt_pub.publish(stop)
            self._sleep(0.1)

        self._publish_activity("stopped", self._carrying_kg)
        self._refuse_queued_work("cancelled by the emergency stop")

    # ------------------------------------------------------------------ outgoing
    def _publish_status(self, order_id, status, attempts, detail=""):
        self.status_pub.publish(String(data=json.dumps({
            "id": order_id, "status": status, "attempts": attempts, "detail": detail,
        })))

    def _publish_activity(self, activity, payload_kg=0.0, order_id=None):
        """Tell the condition monitor what we are doing and what we are carrying."""
        self.activity_pub.publish(String(data=json.dumps({
            "activity": activity, "payload_kg": payload_kg, "order_id": order_id,
        })))

    # ------------------------------------------------------------------- helpers
    def _sleep(self, seconds):
        threading.Event().wait(seconds)

    def _wait(self, future, timeout=180.0):
        end = threading.Event()
        future.add_done_callback(lambda _f: end.set())
        if not end.wait(timeout):
            self.get_logger().error("Timed out waiting for a result.")
            return None
        return future.result()

    def _publish_initial_pose(self, timeout=90.0):
        """Keep telling AMCL where the robot is until it confirms by publishing a pose.

        This has to be a handshake, not fire-and-forget: early in start-up AMCL's TF buffer
        often has no odom->base_footprint yet, so it discards the message. Retrying until
        /amcl_pose arrives removes the race.
        """
        x, y, yaw = self.get_parameter("initial_pose").value
        z, w = yaw_to_quat(yaw)
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = "map"
        msg.pose.pose.position.x = float(x)
        msg.pose.pose.position.y = float(y)
        msg.pose.pose.orientation.z = z
        msg.pose.pose.orientation.w = w
        msg.pose.covariance[0] = 0.25
        msg.pose.covariance[7] = 0.25
        msg.pose.covariance[35] = 0.07

        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            msg.header.stamp = self.get_clock().now().to_msg()
            self.initial_pose_pub.publish(msg)
            if self._amcl_ready.wait(2.0):
                self.get_logger().info(
                    f"Initial pose ({x}, {y}, yaw={yaw}) accepted by AMCL "
                    f"after {attempt} attempt(s).")
                return True
            if attempt % 5 == 0:
                self.get_logger().warn(
                    f"AMCL has not confirmed the initial pose after {attempt} attempts; "
                    "still retrying ...")
        self.get_logger().error(
            f"AMCL never confirmed the initial pose after {timeout:.0f} s.")
        return False

    def navigate_to(self, x, y, yaw=0.0):
        """Send a Nav2 goal and block until it finishes. Returns True on success."""
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        qz, qw = yaw_to_quat(yaw)
        goal.pose.pose.orientation.z = qz
        goal.pose.pose.orientation.w = qw

        # Every route into motion passes through here, which makes it the one place the
        # emergency stop has to be honoured. Checked before sending and again after the result,
        # so a stop raised mid-drive cannot be mistaken for an ordinary navigation failure.
        if self._abort.is_set():
            self.get_logger().warn("Not navigating: the emergency stop is engaged.")
            return False

        self.get_logger().info(f"Navigating to ({x:.2f}, {y:.2f}) ...")

        # Nav2 can still be activating when the action server first appears, and an inactive
        # server rejects goals outright. Retry a few times before giving up.
        handle = None
        for attempt in range(1, 6):
            handle = self._wait(self.nav_client.send_goal_async(goal), timeout=30.0)
            if handle is not None and handle.accepted:
                break
            if self._abort.is_set():
                return False
            self.get_logger().warn(
                f"Nav2 rejected the goal (attempt {attempt}/5); retrying in 3 s ...")
            self._sleep(3.0)
        if handle is None or not handle.accepted:
            self.get_logger().error("Nav2 kept rejecting the goal; giving up.")
            return False

        # Publish the handle so `_halt` can cancel it from the subscription thread. Without
        # this the stop button could not interrupt a drive already under way.
        with self._nav_lock:
            self._nav_handle = handle
        try:
            result = self._wait(handle.get_result_async())
        finally:
            with self._nav_lock:
                self._nav_handle = None

        if result is None:
            return False
        ok = result.status == 4          # 4 == SUCCEEDED in action_msgs/GoalStatus
        if self._abort.is_set():
            self.get_logger().warn("Navigation ended because the emergency stop was engaged.")
            return False
        self.get_logger().info(
            f"Navigation {'succeeded' if ok else 'failed'} (status {result.status}).")
        return ok

    def _facing(self, from_xy, target_xy):
        """Yaw that points from one place at another, or None if the target is unknown."""
        if target_xy is None:
            return None
        return math.atan2(target_xy[1] - from_xy[1], target_xy[0] - from_xy[0])

    def approach(self, point, target, standoff=0.0, timeout=8.0):
        """Drive to `point` FACING `target`, optionally stopping `standoff` metres short.

        Two things the plain goal could not express, and both were costing delivery accuracy:

        * **Facing.** `navigate_to` defaults to yaw 0, so the robot always arrived pointing
          along +x whatever it had come to collect. The gripper then had a parcel off to one
          side, welded it there, and carried it at that offset all the way to the bay.
        * **Standoff.** A parcel rides `carry_offset` in front of the robot centre. Driving to
          the bay centre therefore leaves the parcel a carry-offset BEYOND it. Stopping short
          by exactly that distance puts the parcel on the bay instead of past it.

        The parcel's pose can be unknown on the first approach - PosePublisher is event-driven -
        in which case this waits briefly, then falls back to an unfaced goal rather than refusing
        to move.
        """
        deadline = time.time() + timeout
        while target is None and time.time() < deadline:
            self._sleep(0.5)

        goal = tuple(point)
        if standoff > 0.0 and target is not None:
            # Stand off along the line the robot is ALREADY coming from, not along whatever
            # `point` happens to be. When point == target (the bay) `_facing` would otherwise
            # compute atan2(0, 0) = 0 and always park due east of it - a long way round, and
            # into the west wall for delivery_1.
            approach_from = self.robot_position or goal
            bearing = self._facing(approach_from, target)
            if bearing is not None:
                goal = (target[0] - standoff * math.cos(bearing),
                        target[1] - standoff * math.sin(bearing))
        yaw = self._facing(goal, target)
        return self.navigate_to(goal[0], goal[1], yaw if yaw is not None else 0.0)

    def call_gripper(self, client, item, release=False):
        request = Grab.Request()
        request.item = item
        request.release = release
        return self._wait(client.call_async(request))

    def release_parcel(self, model, attempts=MAX_RELEASE_ATTEMPTS):
        """Put a parcel down and confirm it let go. Returns True only if it did.

        Never treat this as best-effort. A grab that fails leaves the parcel on its shelf and
        costs one order; a release that fails leaves it welded to the gripper and corrupts
        every order afterwards, so it is worth spending several seconds being sure.
        """
        for attempt in range(1, attempts + 1):
            response = self.call_gripper(self.release_client, model, release=True)
            if response is not None and response.success:
                if attempt > 1:
                    self.get_logger().info(f"{model} released on attempt {attempt}.")
                return True
            detail = response.message if response is not None else "the gripper did not answer"
            self.get_logger().warn(
                f"Release attempt {attempt}/{attempts} failed: {detail}")
            self._sleep(1.0)
        return False

    def return_to_station(self):
        """Drive home and report `charging`, which lets the condition model recover."""
        self._publish_activity("returning")
        if self.navigate_to(*self.station):
            self._publish_activity("charging")
            self.get_logger().info("At the station, charging.")
            return True
        self.get_logger().error("Could not reach the station.")
        self._publish_activity("idle")
        return False

    def _drop_carried(self, order_id):
        """Put down whatever the gripper is still holding. True if the robot is now empty.

        Only ever non-empty here after a stop interrupted a delivery mid-carry: normal paths
        clear it themselves. Driving anywhere while loaded is the dragging bug fix 4 exists to
        prevent, so this runs before any task starts, not just before the trip home.
        """
        model = self._carrying_model
        self.get_logger().warn(f"Still holding {model}; putting it down before moving.")
        if not self.release_parcel(model):
            self._parcel_is_stuck(order_id, model, self._carrying_kg)
            return False
        self._carrying_kg, self._carrying_model = 0.0, None
        self._publish_activity("idle")
        return True

    def _stopped(self, order_id):
        """Was that failure actually the emergency stop? If so, say so and do not drive.

        Every failure path in `execute_order` would otherwise try to return to the station,
        which is the one thing a halted robot must not do.
        """
        if not self._abort.is_set():
            return False
        self._publish_status(order_id, "failed", 0,
                             "halted by the emergency stop")
        self.get_logger().error(f"[order {order_id}] abandoned: emergency stop engaged")
        return True

    # --------------------------------------------------------- the stuck-parcel state
    def _parcel_is_stuck(self, order_id, model, payload_kg=0.0):
        """The gripper would not let go. Stop taking work until it does.

        The robot stays where it is rather than driving home: moving would drag the parcel
        across the warehouse, and the station is the one place a stray parcel would block the
        next charge. `_serve_queue` keeps retrying the release from here.

        The payload is reported honestly as still carried - the robot really is holding it, and
        the condition model must not be told the load vanished.
        """
        self._stuck_parcel = model
        detail = (f"release failed - {model} is still attached to the gripper; "
                  "no further orders will run until it lets go")
        self._publish_status(order_id, "failed", 0, detail)
        self._publish_activity("idle", payload_kg, order_id)
        self.get_logger().error(f"[order {order_id}] {detail}")

    def _recover_stuck_parcel(self):
        """Try once more to drop a parcel the gripper would not release.

        Called from the serve loop, so this retries every few seconds for as long as the node
        runs. Detach failures observed so far have been dropped messages rather than a broken
        joint, which means persistence usually does clear them without a restart.
        """
        model = self._stuck_parcel
        if self.release_parcel(model, attempts=1):
            self.get_logger().info(
                f"{model} finally released - resuming normal service.")
            self._stuck_parcel = None
            self._publish_activity("idle")
            return True
        return False

    def _refuse_queued_work(self, reason):
        """Fail everything waiting, instead of letting it sit at 'pending' for ever.

        The alternative is the failure mode this project has already been bitten by: a node
        that is alive and subscribed, so the API keeps accepting orders, but can never run
        any of them - and nothing anywhere says why.
        """
        with self._queue_lock:
            waiting, self._queue = self._queue, []
        for order in waiting:
            self._publish_status(order["id"], "failed", 0, reason)
            self.get_logger().error(f"[order {order['id']}] refused: {reason}")

    # ------------------------------------------------------------- the sequence
    def execute_return(self, order):
        """Drive home because the operator asked, reporting progress like any other order.

        The only reason this is a queued task rather than an immediate command is fix 4: yanking
        the robot out of a delivery would leave a parcel half-carried, and a parcel on the
        gripper corrupts everything after it. So the request waits its turn, and the operator
        watches it sitting at 'pending' on /orders until the robot is free.
        """
        order_id = order["id"]

        self._publish_status(order_id, "navigating", 0, "returning to the station")
        self._publish_activity("returning")
        if not self.navigate_to(*order["dropoff"]):
            if self._stopped(order_id):
                return False
            self._publish_status(order_id, "failed", 0, "could not reach the station")
            self.get_logger().error(f"[task {order_id}] could not reach the station")
            self._publish_activity("idle")
            return False
        # `charging` is what lets the condition model recover battery and cool the motors.
        self._publish_activity("charging")
        self._publish_status(order_id, "completed", 0, "at the station, charging")
        self.get_logger().info(f"[task {order_id}] at the station, charging.")
        return True

    def execute_order(self, order):
        """Run one order: navigate -> grab (up to 3 tries) -> navigate -> release -> verify.

        Once the parcel is on the gripper, EVERY exit from this method has to put it down
        first. Driving away holding it is not a failed order, it is a robot that will get the
        next order wrong too.
        """
        order_id, model, payload = order["id"], order["model"], order.get("weight_kg", 0.0)

        # 1) Drive to the pick point.
        self._publish_activity("working", 0.0, order_id)
        self._publish_status(order_id, "navigating", 0, f"heading to {order['pickup']}")
        if not self.approach(order["pickup"], order.get("shelf")):
            if self._stopped(order_id):
                return False
            self._publish_status(order_id, "failed", 0, "could not reach the pick point")
            self.return_to_station()
            return False

        # 2) Grab it, up to MAX_GRAB_ATTEMPTS times (FR8). No backoff state machine: just
        #    re-approach and try again, because parking too far away is the usual cause and
        #    re-approaching is what actually changes it.
        grabbed = False
        for attempt in range(1, MAX_GRAB_ATTEMPTS + 1):
            self._publish_status(order_id, "grabbing", attempt,
                                 f"attempt {attempt} of {MAX_GRAB_ATTEMPTS}")
            self.get_logger().info(f"[order {order_id}] grab attempt {attempt}")
            response = self.call_gripper(self.grab_client, model)
            if response is not None and response.success:
                grabbed = True
                self.get_logger().info(f"[order {order_id}] {response.message}")
                break
            if response is not None:
                self.get_logger().warn(f"[order {order_id}] {response.message}")
            if attempt < MAX_GRAB_ATTEMPTS:
                self._sleep(2.0)
                # Re-approach FACING the parcel: a retry that parks at the same wrong heading
                # simply fails the bearing check again.
                self.approach(order["pickup"], order.get("shelf"))

        if not grabbed:
            if self._stopped(order_id):
                return False
            self._publish_status(
                order_id, "failed", MAX_GRAB_ATTEMPTS,
                f"could not grab {model} in {MAX_GRAB_ATTEMPTS} attempts; returning to station")
            self.get_logger().error(f"[order {order_id}] grab failed {MAX_GRAB_ATTEMPTS} times")
            self.return_to_station()
            return False
        self._carrying_kg, self._carrying_model = payload, model

        # 3) Carry it to the delivery bay. The robot is now loaded, which the condition model
        #    needs to know: payload drives both energy draw and heating.
        self._publish_activity("working", payload, order_id)
        self._publish_status(order_id, "delivering", MAX_GRAB_ATTEMPTS,
                             f"carrying to {order['dropoff']}")
        if not self.approach(order["dropoff"], order["dropoff"],
                             standoff=self.carry_offset):
            # An emergency stop leaves the parcel exactly where it is, on the gripper. That was
            # the explicit choice: stop dead, move nothing. Releasing here would be a motion the
            # operator did not ask for, and the parcel would end up loose on the floor.
            if self._stopped(order_id):
                return False
            # An ordinary navigation failure is different: put the parcel down HERE rather than
            # carrying it home. A parcel on the floor is a failed order; a parcel still on the
            # gripper is a broken robot.
            self._publish_status(order_id, "failed", 0, "could not reach the delivery bay")
            if not self.release_parcel(model):
                self._parcel_is_stuck(order_id, model, payload)
                return False
            self._carrying_kg, self._carrying_model = 0.0, None
            self._publish_activity("working", 0.0, order_id)
            self.return_to_station()
            return False

        # 4) Put it down, and make sure it really let go before moving anywhere.
        self._publish_status(order_id, "releasing", 0, "releasing the parcel")
        if not self.release_parcel(model):
            self._parcel_is_stuck(order_id, model, payload)
            return False
        self._carrying_kg, self._carrying_model = 0.0, None
        self._publish_activity("working", 0.0, order_id)

        # 5) C3: confirm against the simulator, not against our own commands.
        self._sleep(1.0)
        where = self.parcel_poses.get(model)
        if where is None:
            self._publish_status(order_id, "failed", 0,
                                 f"cannot confirm delivery: no pose for {model}")
            self.return_to_station()
            return False
        gap = distance(where, order["dropoff"])
        if gap > self.delivery_tolerance:
            self._publish_status(
                order_id, "failed", 0,
                f"{model} ended {gap:.2f} m from the bay - it was never carried")
            self.get_logger().error(f"[order {order_id}] NOT delivered ({gap:.2f} m off)")
            self.return_to_station()
            return False

        self._publish_status(order_id, "completed", 0, f"delivered ({gap:.2f} m from target)")
        self.get_logger().info(
            f"[order {order_id}] DELIVERED - {model} is {gap:.2f} m from {order['dropoff']}")
        return True

    # ------------------------------------------------------------------- worker
    def _run(self):
        self.get_logger().info("Task manager starting; waiting for Nav2 and gripper ...")
        self.nav_client.wait_for_server()
        self.grab_client.wait_for_service()
        self.release_client.wait_for_service()
        self.get_logger().info("Nav2 and gripper are up.")

        # Retry localization rather than giving up. Returning here would leave the node alive
        # but inert: still subscribed to /orders/new, so the API keeps accepting orders that
        # can never run, and each sits at 'pending' forever with no error anywhere.
        if self.get_parameter("publish_initial_pose").value:
            rounds = 0
            while not self._publish_initial_pose():
                rounds += 1
                self.get_logger().error(
                    f"Still cannot localize the robot after round {rounds}. NO ORDER CAN RUN "
                    "until AMCL confirms a pose. Retrying ...")
                self._sleep(5.0)
        elif not self._amcl_ready.wait(90.0):
            self.get_logger().warn("No /amcl_pose; set the initial pose in RViz.")
        self._sleep(3.0)        # let the particle filter settle before the first goal

        self.get_logger().info("Localized and ready - orders submitted now will execute.")
        self._publish_activity("charging")      # boots docked on the station
        self._serve_queue()

    def _serve_queue(self):
        """Serve orders oldest-first, and go home when there is nothing to do (FR6, FR11)."""
        idle_since = time.time()
        at_station = True

        while True:
            # Reaching the top of this loop means the worker has regained control, so whatever
            # the stop button interrupted has finished unwinding. That is the moment the abort
            # has done its job, so it is the moment it ends. Nothing stays engaged.
            if self._abort.is_set():
                self._abort.clear()
                self._refuse_queued_work("cancelled by the emergency stop")
                self._publish_activity("idle", self._carrying_kg)
                self.get_logger().warn("Stopped. The robot is idle and will accept new work.")
                continue

            # A parcel stuck on the gripper outranks the queue: starting an order now would
            # carry it to the next shelf and invalidate every delivery after it.
            if self._stuck_parcel is not None:
                if not self._recover_stuck_parcel():
                    self._refuse_queued_work(
                        f"{self._stuck_parcel} is still attached to the gripper - the robot "
                        "cannot take orders until it is released")
                    self._sleep(5.0)
                    continue
                idle_since = time.time()

            with self._queue_lock:
                order = self._queue.pop(0) if self._queue else None

            if order is not None:
                # A stop can leave a parcel on the gripper, and the operator's next move may
                # simply be another order. Starting one while still holding something is the
                # dragging bug, so put it down first whatever the task is.
                if self._carrying_model and not self._drop_carried(order["id"]):
                    continue

                if order["kind"] == KIND_RETURN:
                    # A return that worked leaves the robot docked and charging, so it must not
                    # be marked idle afterwards or the condition model stops recovering - and
                    # the idle timer must not send it home again from where it already is.
                    at_station = self.execute_return(order)
                else:
                    at_station = False
                    self.execute_order(order)
                    # After each delivery the robot is at a bay. Do not drive home yet - another
                    # order may be waiting, and the idle timer below handles the rest.
                    self._publish_activity("idle")
                idle_since = time.time()
                continue

            if not at_station and time.time() - idle_since >= self.idle_return_seconds:
                self.get_logger().info(
                    f"No orders for {self.idle_return_seconds:.0f} s - returning to station.")
                at_station = self.return_to_station()
                idle_since = time.time()

            self._sleep(1.0)


def main():
    rclpy.init()
    node = TaskManager()
    # num_threads is bounded because `MultiThreadedExecutor()` with no argument spawns one
    # worker per CPU - twelve on this machine - for a node that needs two or three. Fewer
    # threads, less context switching, same behaviour.
    #
    # It does NOT fix this node's CPU use, and it was measured rather than assumed: the node
    # still burns most of a core because /clock is published at ~500 Hz and every node with
    # use_sim_time processes all of it. See HANDOVER 5.13 for that, which is the real cost.
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
