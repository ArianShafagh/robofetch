"""Task Manager - executes a delivery order end to end.

Chains the full pick-and-place sequence described in the proposal (UC4):

    navigate to A  ->  grab  ->  navigate to B  ->  release

Navigation is delegated to Nav2's NavigateToPose action (a third-party service, used
as-is) and grabbing to the gripper node's services. The orchestration, state tracking
and error handling are ours.

Orders are served using the nearest-neighbour scheduler (M5): whenever the robot is
idle, the pending order whose pickup point is closest is chosen next. The grab-retry
state machine (M6) plugs into `execute_order` without changing this structure.

The sequence runs on a worker thread and blocks on futures, while a MultiThreadedExecutor
services callbacks - much easier to follow than a callback-chained state machine.
"""
import json
import math
import threading
import time

import rclpy
from geometry_msgs.msg import Pose, PoseWithCovarianceStamped, PoseStamped
from nav2_msgs.action import NavigateToPose
from std_msgs.msg import String
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from robofetch_interfaces.srv import Grab
from robofetch_core.order import Order, OrderState
from robofetch_core.scheduler import distance, pending_orders, select_next_order
from robofetch_core.retry import (DEFAULT_MAX_ATTEMPTS, GrabDecision,
                                  backoff_seconds, decide_after_grab,
                                  describe, next_state)


def yaw_to_quat(yaw):
    """Minimal yaw -> quaternion (z, w); roll and pitch are always zero here."""
    return math.sin(yaw / 2.0), math.cos(yaw / 2.0)


class TaskManager(Node):
    def __init__(self):
        super().__init__("task_manager")

        # Orders to run, each encoded "item,pickup_x,pickup_y,dropoff_x,dropoff_y".
        # Coordinates are map coordinates, which - because the map is generated from the
        # world geometry - are also the Gazebo world coordinates of the markers.
        #
        # The defaults are deliberately submitted in a NON-optimal sequence so the
        # nearest-neighbour scheduler visibly reorders them (M5 acceptance test).
        # M7 replaces this parameter with orders arriving from the web API.
        # Warehouse layout: three SHELVES are the pickup points, one DELIVERY STATION
        # (south-west) is where parcels go. Each order gets its own spot inside the
        # station: the parcels are shorter than the lidar so Nav2 cannot see them, and
        # stacking every delivery on one point makes the robot drive into a parcel it
        # already dropped.
        #
        #   shelf_1 (north-west)  item_1   pickup (-2.5,  0.95)
        #   shelf_2 (north-east)  item_2   pickup ( 1.5,  0.95)
        #   shelf_3 (east)        item_3   pickup ( 2.75, -1.0)
        #
        # Submitted in a deliberately NON-optimal order so the nearest-neighbour
        # scheduler visibly reorders them. M7 replaces this with orders from the web API.
        # A SEMICOLON-SEPARATED string, not a list: ROS 2 cannot infer the type of an
        # EMPTY array parameter, and launching with no pre-queued orders (the normal case
        # once the web API is running) made the whole launch abort with
        # "Expected a non-empty sequence ... inconsistent input". A plain string is
        # always well-typed, empty or not.
        #   "item,pickup_x,pickup_y,dropoff_x,dropoff_y; item,..."
        self.declare_parameter("orders", "")
        # The robot spawns on the delivery station; publishing that once lets AMCL localize without
        # a human clicking "2D Pose Estimate" in RViz.
        self.declare_parameter("publish_initial_pose", True)
        self.declare_parameter("initial_pose", [-2.6, -2.0, 0.0])   # x, y, yaw
        # How close the parcel must end up to count as delivered. 0.45 m is achievable
        # now that (a) the gripper snaps each parcel to a known carry position instead of
        # welding it wherever it lay, and (b) the robot parks so the PARCEL, not its own
        # centre, lands on the target. Error budget: 0.20 m parking + ~0.10 m from
        # heading error + settling, with margin.
        self.declare_parameter("delivery_tolerance", 1.1)
        # Distance from the robot centre to the carried parcel. The robot is aimed so the
        # PARCEL lands on the drop-off, not the robot centre - otherwise every delivery is
        # off by this whole offset. Must match the gripper node's carry_offset.
        self.declare_parameter("carry_offset", 0.55)
        # Heading the robot faces when placing a parcel (radians). pi = facing west,
        # which keeps the parking spot on the open side of the delivery station.
        self.declare_parameter("approach_yaw", 3.14159)
        # FR4: retry a failed grab up to this many times before failing the order.
        self.declare_parameter("max_grab_attempts", DEFAULT_MAX_ATTEMPTS)
        # True when the web API supplies orders: the task manager then keeps running and
        # waiting instead of exiting once the initial queue is empty.
        self.declare_parameter("run_forever", False)

        self.orders = self._parse_orders(self.get_parameter("orders").value)
        self.delivery_tolerance = self.get_parameter("delivery_tolerance").value
        self.carry_offset = self.get_parameter("carry_offset").value
        self.approach_yaw = self.get_parameter("approach_yaw").value
        self.max_grab_attempts = self.get_parameter("max_grab_attempts").value
        self.run_forever = self.get_parameter("run_forever").value
        # Robot position the scheduler measures from: seeded with the declared start
        # pose, then kept up to date from AMCL as the robot moves.
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

        # AMCL only publishes once it is active and has a pose estimate, which makes it
        # a reliable "the navigation stack is really ready" signal. The action server
        # appearing is not enough: Nav2's lifecycle nodes reject goals until activated.
        self._amcl_ready = threading.Event()
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose",
                                 self._on_amcl_pose, 10)

        # Item world poses, used to CONFIRM a delivery physically happened. Reporting a
        # delivery because the commands returned success is not enough: a grab that
        # silently failed leaves the parcel behind while every step still reports OK.
        self.item_poses = {}
        for order in self.orders:
            self.create_subscription(
                Pose, f"/model/{order.item}/pose",
                lambda msg, n=order.item: self.item_poses.__setitem__(
                    n, (msg.position.x, msg.position.y)),
                10)

        # --- link to the web bridge (M7) ---
        # Orders arrive as JSON on /orders/new and every state change is published on
        # /orders/status. Topics rather than a service because status is a stream the
        # bridge and the dashboard both follow, and a dropped order must not block the
        # HTTP request that created it.
        self.status_pub = self.create_publisher(String, "/orders/status", 10)
        self.create_subscription(String, "/orders/new", self._on_new_order, 10)
        self._orders_lock = threading.Lock()
        self._next_local_id = len(self.orders) + 1

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _on_new_order(self, msg):
        """Queue an order submitted through the web API.

        Payload: {"id", "item", "pickup": [x, y], "dropoff": [x, y]}
        The bridge resolves waypoint NAMES to coordinates, so the robot side only ever
        deals in numbers.
        """
        try:
            data = json.loads(msg.data)
            order = Order(order_id=int(data["id"]), item=data["item"],
                          pickup=tuple(data["pickup"]), dropoff=tuple(data["dropoff"]))
        except (ValueError, KeyError, TypeError) as exc:
            self.get_logger().error(f"Ignoring malformed order '{msg.data}': {exc}")
            return

        with self._orders_lock:
            if any(o.order_id == order.order_id for o in self.orders):
                return                      # already queued; ignore duplicates
            self.orders.append(order)
            # Watch this item's pose so the delivery can be physically verified.
            if order.item not in self.item_poses:
                self.create_subscription(
                    Pose, f"/model/{order.item}/pose",
                    lambda m, n=order.item: self.item_poses.__setitem__(
                        n, (m.position.x, m.position.y)),
                    10)
        self.get_logger().info(
            f"Accepted order {order.order_id}: {order.item} "
            f"{order.pickup} -> {order.dropoff}")
        self._publish_status(order)

    def _publish_status(self, order):
        """Tell the bridge (and through it the client) about an order's state."""
        self.status_pub.publish(String(data=json.dumps({
            "id": order.order_id,
            "item": order.item,
            "status": order.state.value,
            "retries": order.attempts,
            "detail": order.detail,
        })))

    # ------------------------------------------------------------------ helpers
    def _on_amcl_pose(self, msg):
        """Track where the robot is; the scheduler measures distances from here."""
        self._amcl_ready.set()
        p = msg.pose.pose.position
        self.robot_position = (p.x, p.y)

    def _parse_orders(self, spec_string):
        """Turn a "item,px,py,dx,dy; item,..." string into Order objects."""
        specs = [s for s in (part.strip() for part in spec_string.split(";")) if s]
        orders = []
        for index, spec in enumerate(specs, start=1):
            try:
                item, px, py, dx, dy = [s.strip() for s in spec.split(",")]
                orders.append(Order(order_id=index, item=item,
                                    pickup=(float(px), float(py)),
                                    dropoff=(float(dx), float(dy))))
            except ValueError:
                self.get_logger().error(
                    f"Ignoring malformed order '{spec}'; "
                    "expected 'item,pickup_x,pickup_y,dropoff_x,dropoff_y'")
        return orders

    def _publish_initial_pose(self, timeout=90.0):
        """Keep telling AMCL where the robot is until it confirms by publishing a pose.

        This has to be a handshake, not a fire-and-forget. Early in start-up AMCL's TF
        buffer often has no odom->base_footprint yet, so it logs "Failed to transform
        initial pose in time" and DISCARDS the message. If we stop after a fixed number
        of attempts and they all land in that window, AMCL never localizes and just
        repeats "AMCL cannot publish a pose ... Please set the initial pose", while the
        robot sits still forever. Retrying until /amcl_pose arrives removes the race.
        """
        x, y, yaw = self.get_parameter("initial_pose").value
        z, w = yaw_to_quat(yaw)
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = "map"
        msg.pose.pose.position.x = float(x)
        msg.pose.pose.position.y = float(y)
        msg.pose.pose.orientation.z = z
        msg.pose.pose.orientation.w = w
        # A little covariance so AMCL treats it as a hint, not gospel.
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

    def _sleep(self, seconds):
        """Sleep without blocking the executor (we are on a worker thread)."""
        threading.Event().wait(seconds)

    def navigate_to(self, x, y, yaw=0.0):
        """Send a Nav2 goal and block until it finishes. Returns True on success."""
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        qz, qw = yaw_to_quat(yaw)
        goal.pose.pose.orientation.z = qz
        goal.pose.pose.orientation.w = qw

        self.get_logger().info(f"Navigating to ({x:.2f}, {y:.2f}) ...")

        # Nav2 can still be activating when the action server first appears, and an
        # inactive server rejects goals outright. Retry a few times before giving up.
        handle = None
        for attempt in range(1, 6):
            handle = self._wait(self.nav_client.send_goal_async(goal), timeout=30.0)
            if handle is not None and handle.accepted:
                break
            self.get_logger().warn(
                f"Nav2 rejected the goal (attempt {attempt}/5); retrying in 3 s ...")
            self._sleep(3.0)
        if handle is None or not handle.accepted:
            self.get_logger().error("Nav2 kept rejecting the goal; giving up.")
            return False

        result = self._wait(handle.get_result_async())
        if result is None:
            return False
        # status 4 == SUCCEEDED in action_msgs/GoalStatus
        ok = result.status == 4
        self.get_logger().info(f"Navigation {'succeeded' if ok else 'failed'} "
                               f"(status {result.status}).")
        return ok

    def call_gripper(self, client, item, release=False):
        """Call grab or release for `item`; returns the response (or None on failure)."""
        request = Grab.Request()
        request.item = item
        request.release = release
        return self._wait(client.call_async(request))

    def _wait(self, future, timeout=180.0):
        """Block the worker thread until a future completes."""
        end = threading.Event()
        future.add_done_callback(lambda _f: end.set())
        if not end.wait(timeout):
            self.get_logger().error("Timed out waiting for a result.")
            return None
        return future.result()

    # ------------------------------------------------------------- the sequence
    def execute_order(self, order: Order) -> bool:
        """Run one order: navigate -> grab -> navigate -> release."""
        # 1) Drive to the pickup point.
        self._set_state(order, OrderState.NAVIGATING, f"heading to pickup {order.pickup}")
        self.get_logger().info(f"[order {order.order_id}] {order.detail}")
        if not self.navigate_to(*order.pickup):
            self._set_state(order, OrderState.FAILED, "could not reach the pickup point")
            return False

        # 2) Grab the item, retrying on failure (Complex Functionality 2 / FR4).
        if not self._grab_with_retries(order):
            return False

        # 3) Carry it to the drop-off point.
        self._set_state(order, OrderState.DELIVERING, f"carrying to {order.dropoff}")
        self.get_logger().info(f"[order {order.order_id}] {order.detail}")
        if not self.navigate_to(*order.dropoff):
            self._set_state(order, OrderState.FAILED, "could not reach the drop-off point")
            return False

        # 4) Put it down.
        self._set_state(order, OrderState.RELEASING, "releasing the item")
        response = self.call_gripper(self.release_client, order.item, release=True)
        if response is None or not response.success:
            self._set_state(order, OrderState.FAILED, "release failed")
            return False

        # Physically confirm the parcel is at the drop-off point.
        self._sleep(1.0)
        where = self.item_poses.get(order.item)
        if where is None:
            self._set_state(order, OrderState.FAILED,
                            f"cannot confirm delivery: no pose for {order.item}")
            self.get_logger().error(f"[order {order.order_id}] {order.detail}")
            return False
        gap = distance(where, order.dropoff)
        if gap > self.delivery_tolerance:
            self._set_state(order, 
                OrderState.FAILED,
                f"{order.item} ended {gap:.2f} m from the drop-off - it was never carried")
            self.get_logger().error(f"[order {order.order_id}] {order.detail}")
            return False

        self._set_state(order, OrderState.COMPLETED, f"delivered ({gap:.2f} m from target)")
        self.get_logger().info(
            f"[order {order.order_id}] DELIVERED - {order.item} is {gap:.2f} m from "
            f"{order.dropoff}")
        return True

    def _set_state(self, order, state, detail=""):
        """Record a transition AND push it to the bridge, so the client sees it live."""
        order.set_state(state, detail)
        self._publish_status(order)

    def _grab_with_retries(self, order):
        """Attempt the grab, retrying up to max_grab_attempts times (FR4).

        Between attempts the robot backs off and DRIVES TO THE PICKUP POINT AGAIN rather
        than simply waiting. The usual reason a grab fails is that the robot parked too
        far from the parcel, and re-approaching is what actually changes that; waiting
        alone would just fail again identically.

        The decision of retry-vs-give-up lives in robofetch_core.retry so it can be unit
        tested; this method only carries the decision out.
        """
        while True:
            self._set_state(order, OrderState.GRABBING,
                            f"attempt {order.attempts + 1} of {self.max_grab_attempts}")
            self.get_logger().info(f"[order {order.order_id}] {order.detail}")

            order.attempts += 1
            response = self.call_gripper(self.grab_client, order.item, release=False)
            success = response is not None and response.success
            if response is not None and not success:
                self.get_logger().warn(f"[order {order.order_id}] {response.message}")

            decision = decide_after_grab(success, order.attempts, self.max_grab_attempts)
            detail = describe(decision, order.item, order.attempts,
                              self.max_grab_attempts)
            self._set_state(order, next_state(decision), detail)

            if decision is GrabDecision.PROCEED:
                self.get_logger().info(f"[order {order.order_id}] {detail}")
                return True

            if decision is GrabDecision.GIVE_UP:
                self.get_logger().error(f"[order {order.order_id}] {detail}")
                return False

            # RETRY: wait, then re-approach the pickup point before trying again.
            wait = backoff_seconds(order.attempts)
            self.get_logger().warn(
                f"[order {order.order_id}] {detail} (waiting {wait:.0f} s)")
            self._sleep(wait)
            if not self.navigate_to(*order.pickup):
                self._set_state(order, OrderState.FAILED,
                                "could not re-approach the pickup point for a retry")
                self.get_logger().error(f"[order {order.order_id}] {order.detail}")
                return False

    def _run(self):
        """Worker thread: wait for the stack, then execute the single M4 order."""
        self.get_logger().info("Task manager starting; waiting for Nav2 and gripper ...")
        self.nav_client.wait_for_server()
        self.grab_client.wait_for_service()
        self.release_client.wait_for_service()
        self.get_logger().info("Nav2 and gripper are up.")

        # Localize before doing anything else. The handshake blocks until AMCL confirms,
        # which also proves the localization half of Nav2 is active and will accept goals.
        #
        # This RETRIES instead of giving up. Returning from this thread used to leave the
        # node alive but inert: it stayed subscribed to /orders/new, so the web API kept
        # accepting orders perfectly happily, and every one of them sat at 'pending'
        # forever with no error in the API, the database or the client's response. From
        # the second terminal that is indistinguishable from a working system.
        #
        # The condition is also usually TEMPORARY - AMCL's 90 s budget expires when the
        # machine is loaded (a colcon build that just finished, Gazebo and Nav2 still
        # starting), and it localizes fine a few seconds later. Giving up permanently on a
        # recoverable startup race is the worst of both worlds, so keep trying.
        if self.get_parameter("publish_initial_pose").value:
            round_number = 0
            while not self._publish_initial_pose():
                round_number += 1
                self.get_logger().error(
                    f"Still cannot localize the robot after round {round_number}. "
                    "NO ORDER CAN RUN until AMCL confirms a pose - submitted orders will "
                    "stay 'pending'. Is the map server up and the map correct? Retrying ...")
                self._sleep(5.0)
        elif not self._amcl_ready.wait(90.0):
            self.get_logger().warn("No /amcl_pose; set the initial pose in RViz.")
        self._sleep(3.0)     # let the particle filter settle before the first goal

        self.get_logger().info("Localized and ready - orders submitted now will execute.")
        self._run_queue()

    def _run_queue(self):
        """Serve every pending order, choosing the nearest one each time (UC3).

        The scheduler is re-run after each delivery rather than fixing a route up front,
        so the decision always reflects where the robot actually ended up - and so newly
        arriving orders (M7) are considered automatically.
        """
        submitted = [o.item for o in self.orders]
        self.get_logger().info(f"Queue submitted in this order: {submitted}")

        served = []
        idle_logged = False
        while True:
            with self._orders_lock:
                waiting = pending_orders(self.orders)
            if not waiting:
                if not self.run_forever:
                    break
                # Driven by the web API: stay alive and wait for the next order.
                if not idle_logged:
                    self.get_logger().info("Queue empty; waiting for new orders ...")
                    idle_logged = True
                self._sleep(1.0)
                continue
            idle_logged = False

            order = select_next_order(self.robot_position, self.orders)
            gap = distance(self.robot_position, order.pickup)
            others = ", ".join(
                f"{o.item}@{distance(self.robot_position, o.pickup):.2f}m"
                for o in waiting if o is not order)
            self.get_logger().info(
                f"SCHEDULER: at ({self.robot_position[0]:.2f}, "
                f"{self.robot_position[1]:.2f}) -> choosing {order.item} "
                f"({gap:.2f} m away)" + (f"; passed over {others}" if others else ""))

            self.execute_order(order)
            served.append(order.item)
            # After a delivery the robot is at that order's drop-off point; AMCL keeps
            # self.robot_position current, so the next choice starts from there.

        self.get_logger().info(f"Queue finished. Submitted {submitted}, served {served}.")
        for order in self.orders:
            self.get_logger().info(
                f"  order {order.order_id} ({order.item}): {order.state.value} "
                f"- {order.detail}")


def main():
    rclpy.init()
    node = TaskManager()
    executor = MultiThreadedExecutor()
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
