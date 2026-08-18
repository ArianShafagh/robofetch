"""ROS 2 side of the web bridge.

A single rclpy node that the FastAPI process owns:

    publishes  /orders/new       - orders the admission workflow accepted
    subscribes /orders/status    - state changes coming back from the task manager
    subscribes /robot/telemetry  - battery, temperature, condition from the state model
    subscribes /amcl_pose        - the robot's position

It runs on a background thread inside the FastAPI process, so an HTTP handler can publish a
ROS message directly.

Note on `/amcl_pose`: the web pages deliberately do **not** show where the robot is - the
operator places orders, they do not watch the robot drive. The position is still needed
internally, because admission control has to measure the route from wherever the robot
actually is before it can cost the job.

Everything crossing the boundary is JSON on std_msgs/String, which keeps the web layer free of
custom ROS message dependencies.
"""
import json
import threading

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String


class RosLink(Node):
    def __init__(self, on_status=None, on_telemetry=None):
        super().__init__("robofetch_bridge")
        self._on_status = on_status
        self._on_telemetry = on_telemetry

        self.order_pub = self.create_publisher(String, "/orders/new", 10)
        self.estop_pub = self.create_publisher(String, "/robot/estop", 10)
        self.create_subscription(String, "/orders/status", self._handle_status, 10)
        self.create_subscription(String, "/robot/telemetry", self._handle_telemetry, 10)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose",
                                 self._handle_pose, 10)

        # Internal only - used to measure routes, never rendered in the UI.
        self.robot_xy = None
        self.telemetry = {}

        self.get_logger().info(
            "Bridge node up: /orders/new, /orders/status, /robot/telemetry, /amcl_pose")

    # ------------------------------------------------------------------ outgoing
    def submit_order(self, order_id, product, pickup, dropoff):
        """Hand an accepted order to the robot.

        The product is resolved to a simulator model name and a weight here, so the robot side
        deals only in numbers and model names - it never needs the catalogue.
        """
        self.order_pub.publish(String(data=json.dumps({
            "id": order_id,
            "product_id": product["product_id"],
            "model": product["model_name"],
            "weight_kg": product["weight_kg"],
            "pickup": list(pickup),
            "dropoff": list(dropoff),
            # Where the shelf is, so the robot can face the parcel when it parks. It cannot work
            # this out from the parcel's own pose: /model/<parcel>/pose is event-driven and a
            # parcel sitting still on a shelf never publishes, so that pose is simply absent.
            "shelf": [product.get("shelf_x"), product.get("shelf_y")],
        })))
        self.get_logger().info(
            f"Submitted order {order_id} ({product['product_id']}) to the robot.")

    def submit_return(self, order_id, station):
        """Hand a "go home" task to the robot on the SAME topic as a delivery.

        Same topic, same queue, same FIFO position - which is the point. A separate "drive home
        now" channel would let the operator interrupt a delivery mid-carry, and a parcel abandoned
        on the gripper is the failure this system works hardest to avoid.
        """
        self.order_pub.publish(String(data=json.dumps({
            "id": order_id,
            "kind": "return",
            "dropoff": [station["x"], station["y"]],
        })))
        self.get_logger().info(f"Submitted return-to-station task {order_id} to the robot.")

    def publish_estop(self, action):
        """Engage ("stop") or release ("clear") the emergency stop.

        Its own topic rather than the order queue, and this is the one case where bypassing the
        queue is right: an emergency stop that waited its turn behind a delivery would not be an
        emergency stop. Returns the subscriber count so the caller can tell the operator when
        nothing was listening - a stop button that silently reached nobody is worse than none.
        """
        self.estop_pub.publish(String(data=json.dumps({"action": action})))
        self.get_logger().warn(f"Published emergency-stop action '{action}'.")
        return self.estop_pub.get_subscription_count()

    # ------------------------------------------------------------------ incoming
    def _handle_status(self, msg):
        try:
            payload = json.loads(msg.data)
        except ValueError:
            self.get_logger().warn(f"Ignoring malformed status '{msg.data}'")
            return
        if self._on_status:
            self._on_status(payload)

    def _handle_telemetry(self, msg):
        try:
            payload = json.loads(msg.data)
        except ValueError:
            return
        self.telemetry = payload
        if self._on_telemetry:
            self._on_telemetry(payload)

    def _handle_pose(self, msg):
        p = msg.pose.pose.position
        self.robot_xy = (p.x, p.y)


class RosThread:
    """Runs an rclpy executor on a daemon thread alongside FastAPI."""

    def __init__(self, on_status=None, on_telemetry=None):
        rclpy.init()
        self.node = RosLink(on_status=on_status, on_telemetry=on_telemetry)
        # Bounded to what this node needs - see gripper_node.main. It only fans out
        # status and telemetry callbacks.
        self._executor = MultiThreadedExecutor(num_threads=2)
        self._executor.add_node(self.node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._executor.shutdown()
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
