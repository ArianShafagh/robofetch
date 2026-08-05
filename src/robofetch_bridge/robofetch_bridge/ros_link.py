"""ROS 2 side of the web bridge.

A single rclpy node that the FastAPI process owns:

    publishes  /orders/new     - orders submitted through the REST API
    subscribes /orders/status  - state changes coming back from the Task Manager
    subscribes /amcl_pose      - the robot's live position, for the map view

It runs on a background thread inside the FastAPI process (the pattern the proposal
describes), so HTTP handlers can publish ROS messages directly.

Everything crossing the boundary is JSON on std_msgs/String. That keeps the web layer
free of custom ROS message dependencies, which matters because the frontend ultimately
consumes the same payloads over the WebSocket.
"""
import json
import math
import threading

import rclpy
from geometry_msgs.msg import Pose, PoseWithCovarianceStamped
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String

# Parcels whose world pose is forwarded to the dashboard. These are the items that have a
# DetachableJoint and a PosePublisher in warehouse.sdf; the names must match it.
ITEMS = ("item_1", "item_2", "item_3")

# Only forward a parcel once it has moved this far since the last message. A carried
# parcel otherwise streams at the PosePublisher's 20 Hz, which is far more than a map view
# needs and puts pointless traffic on every open WebSocket.
ITEM_MOVE_EPSILON = 0.02        # metres


class RosLink(Node):
    def __init__(self, on_status=None, on_pose=None, on_item=None):
        super().__init__("robofetch_bridge")
        self._on_status = on_status
        self._on_pose = on_pose
        self._on_item = on_item

        self.order_pub = self.create_publisher(String, "/orders/new", 10)
        self.create_subscription(String, "/orders/status", self._handle_status, 10)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose",
                                 self._handle_pose, 10)

        # The parcels' REAL positions in Gazebo. The dashboard draws these rather than
        # inferring where a parcel "should" be from the order status, so the map shows
        # physical reality and not just what the software believes - the whole point of
        # HANDOVER 5.4, made visible in the browser.
        self.item_poses = {}
        for item in ITEMS:
            self.create_subscription(
                Pose, f"/model/{item}/pose",
                lambda msg, name=item: self._handle_item(name, msg), 10)

        self.robot_pose = {"x": 0.0, "y": 0.0}
        self.get_logger().info(
            "Bridge node up: /orders/new, /orders/status, /amcl_pose, "
            f"/model/<item>/pose for {list(ITEMS)}")

    # ------------------------------------------------------------------ outgoing
    def submit_order(self, order_id, item, pickup, dropoff):
        """Send an accepted order to the Task Manager."""
        self.order_pub.publish(String(data=json.dumps({
            "id": order_id, "item": item,
            "pickup": list(pickup), "dropoff": list(dropoff),
        })))
        self.get_logger().info(f"Submitted order {order_id} ({item}) to the robot.")

    # ------------------------------------------------------------------ incoming
    def _handle_status(self, msg):
        try:
            payload = json.loads(msg.data)
        except ValueError:
            self.get_logger().warn(f"Ignoring malformed status '{msg.data}'")
            return
        if self._on_status:
            self._on_status(payload)

    def _handle_pose(self, msg):
        p = msg.pose.pose.position
        self.robot_pose = {"x": p.x, "y": p.y}
        if self._on_pose:
            self._on_pose(self.robot_pose)

    def _handle_item(self, name, msg):
        """Cache a parcel's world pose, forwarding it only when it has actually moved."""
        previous = self.item_poses.get(name)
        moved = previous is None or math.hypot(msg.position.x - previous["x"],
                                               msg.position.y - previous["y"]) > ITEM_MOVE_EPSILON
        self.item_poses[name] = {"x": msg.position.x, "y": msg.position.y}
        if moved and self._on_item:
            self._on_item({"name": name, **self.item_poses[name]})


class RosThread:
    """Runs an rclpy executor on a daemon thread alongside FastAPI."""

    def __init__(self, on_status=None, on_pose=None, on_item=None):
        rclpy.init()
        self.node = RosLink(on_status=on_status, on_pose=on_pose, on_item=on_item)
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(self.node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._executor.shutdown()
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
