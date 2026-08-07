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
from geometry_msgs.msg import Pose, PoseWithCovarianceStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

from robofetch_interfaces.srv import Grab

MAX_GRAB_ATTEMPTS = 3


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
        # How close the parcel must end up to count as delivered. The dominant error is that
        # DetachableJoint welds the parcel wherever it lies, often ~0.5 m off-centre, and
        # releases it at that same offset.
        self.declare_parameter("delivery_tolerance", 1.1)
        # Seconds with an empty queue before the robot drives home (FR11).
        self.declare_parameter("idle_return_seconds", 120.0)

        self.station = tuple(float(v) for v in self.get_parameter("station").value)
        self.delivery_tolerance = self.get_parameter("delivery_tolerance").value
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

        self._queue = []                     # FIFO, oldest first
        self._queue_lock = threading.Lock()
        self._seen_ids = set()

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ incoming
    def _on_new_order(self, msg):
        """Queue an order sent by the web tier.

        Payload: {"id", "product_id", "model", "weight_kg", "pickup": [x, y],
                  "dropoff": [x, y]}
        The bridge resolves the product to coordinates, so this side stays numeric.
        """
        try:
            order = json.loads(msg.data)
            order["pickup"] = tuple(order["pickup"])
            order["dropoff"] = tuple(order["dropoff"])
            order["id"] = int(order["id"])
        except (ValueError, KeyError, TypeError) as exc:
            self.get_logger().error(f"Ignoring malformed order '{msg.data}': {exc}")
            return

        with self._queue_lock:
            if order["id"] in self._seen_ids:
                return                                   # duplicate; ignore
            self._seen_ids.add(order["id"])
            self._queue.append(order)                    # FIFO: append, pop from the front

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

        self.get_logger().info(f"Navigating to ({x:.2f}, {y:.2f}) ...")

        # Nav2 can still be activating when the action server first appears, and an inactive
        # server rejects goals outright. Retry a few times before giving up.
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
        ok = result.status == 4          # 4 == SUCCEEDED in action_msgs/GoalStatus
        self.get_logger().info(
            f"Navigation {'succeeded' if ok else 'failed'} (status {result.status}).")
        return ok

    def call_gripper(self, client, item, release=False):
        request = Grab.Request()
        request.item = item
        request.release = release
        return self._wait(client.call_async(request))

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

    # ------------------------------------------------------------- the sequence
    def execute_order(self, order):
        """Run one order: navigate -> grab (up to 3 tries) -> navigate -> release -> verify."""
        order_id, model, payload = order["id"], order["model"], order.get("weight_kg", 0.0)

        # 1) Drive to the pick point.
        self._publish_activity("working", 0.0, order_id)
        self._publish_status(order_id, "navigating", 0, f"heading to {order['pickup']}")
        if not self.navigate_to(*order["pickup"]):
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
                self.navigate_to(*order["pickup"])       # re-approach before trying again

        if not grabbed:
            self._publish_status(
                order_id, "failed", MAX_GRAB_ATTEMPTS,
                f"could not grab {model} in {MAX_GRAB_ATTEMPTS} attempts; returning to station")
            self.get_logger().error(f"[order {order_id}] grab failed {MAX_GRAB_ATTEMPTS} times")
            self.return_to_station()
            return False

        # 3) Carry it to the delivery bay. The robot is now loaded, which the condition model
        #    needs to know: payload drives both energy draw and heating.
        self._publish_activity("working", payload, order_id)
        self._publish_status(order_id, "delivering", MAX_GRAB_ATTEMPTS,
                             f"carrying to {order['dropoff']}")
        if not self.navigate_to(*order["dropoff"]):
            self._publish_status(order_id, "failed", 0, "could not reach the delivery bay")
            self.return_to_station()
            return False

        # 4) Put it down.
        self._publish_status(order_id, "releasing", 0, "releasing the parcel")
        response = self.call_gripper(self.release_client, model, release=True)
        if response is None or not response.success:
            self._publish_status(order_id, "failed", 0, "release failed")
            self.return_to_station()
            return False
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
            with self._queue_lock:
                order = self._queue.pop(0) if self._queue else None

            if order is not None:
                at_station = False
                self.execute_order(order)
                idle_since = time.time()
                # After each order the robot is at a delivery bay. Do not drive home yet -
                # another order may be waiting, and the idle timer below handles the rest.
                self._publish_activity("idle")
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
