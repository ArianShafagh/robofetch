"""Gripper node - wraps the Gazebo DetachableJoint as ROS services.

Exposes:
    ~/grab     (robofetch_interfaces/srv/Grab)  attach an item to the gripper
    ~/release  (robofetch_interfaces/srv/Grab)  detach it again

Both are the same service type; set `release: true` to drop instead of pick up.

The node does not simply fire the command and claim success. It reads the item's and
the robot's poses (bridged from Gazebo on /model_poses) and checks afterwards whether
the item really is being carried. That verification is what lets the M6 retry state
machine detect a failed grab and try again, per FR4 of the proposal.

Gazebo note: gz-sim 8 creates a DetachableJoint already attached and offers no
"suppress initial attach" option, so on start-up we publish one detach to guarantee the
robot begins empty-handed.
"""
import math
import subprocess
import time

import rclpy
from geometry_msgs.msg import Pose, PoseWithCovarianceStamped
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from std_msgs.msg import Empty, String

from robofetch_interfaces.srv import Grab


class GripperNode(Node):
    def __init__(self):
        super().__init__("gripper_node")

        # How close the item must be to the gripper to count as "held".
        # Nominal parking gap is ~0.35 m, but Nav2 only guarantees 0.25 m goal
        # tolerance, so anything under ~0.7 m produces spurious grab failures.
        self.declare_parameter("hold_tolerance", 0.85)
        # How long to wait after commanding attach/detach before verifying.
        self.declare_parameter("settle_time", 1.0)
        self.declare_parameter("robot_model", "robofetch")
        # EVERY item that has a DetachableJoint on the robot must be listed here.
        # Gazebo creates all of those joints ALREADY ATTACHED at start-up, so any item we
        # forget stays welded to the gripper from wherever it sits - metres away - and
        # anchors the robot: the wheels turn, odometry advances, but the robot cannot
        # actually move. Releasing all of them once at start-up is what frees it.
        self.declare_parameter("items", ["item_1", "item_2", "item_3"])
        # Gazebo world name, needed to call its set_pose service.
        self.declare_parameter("world", "warehouse")
        # Where a grabbed parcel is placed relative to the robot centre, in metres
        # forward. The gripper sits at ~0.39 m; the parcel is snapped just beyond it.
        self.declare_parameter("carry_offset", 0.55)

        self.hold_tolerance = self.get_parameter("hold_tolerance").value
        self.settle_time = self.get_parameter("settle_time").value
        self.robot_model = self.get_parameter("robot_model").value
        self.world = self.get_parameter("world").value
        self.carry_offset = self.get_parameter("carry_offset").value

        self.poses = {}          # model name -> (x, y, z), world frame
        self.held_item = None    # name of the item currently carried, if any

        # Each item publishes its own world pose (PosePublisher plugin), so we always
        # know which pose belongs to which item.
        self._pose_subs = {}
        self._watch_model(self.robot_model)
        self.items = list(self.get_parameter("items").value)
        for item in self.items:
            self._watch_model(item)

        # AUTHORITATIVE attach state, straight from the DetachableJoint's <output_topic>
        # (bridged for every item in bridge.yaml). It publishes the literal strings
        # "attached" / "detached".
        #
        # This is what makes a grab check meaningful. Comparing the gap before and after an
        # attach cannot distinguish "the joint took hold" from "the command did nothing",
        # because in the second case NEITHER body moves and the after-check passes
        # trivially - the same shape of bug as the "3/3 delivered" incident in HANDOVER 5.4,
        # one layer further down. The joint's own state has no such blind spot.
        #
        # The topic fires only on TRANSITIONS, so these subscriptions must exist before the
        # start-up detach below - which is why they are created here in the constructor and
        # not lazily inside on_grab.
        self.attach_state = {}
        self._state_subs = {}
        for item in self.items:
            self._watch_state(item)

        # The robot's position comes from AMCL rather than its Gazebo PosePublisher.
        # PosePublisher only emits on change and was observed going quiet mid-run, which
        # left the gripper comparing against a stale start-of-run position. AMCL
        # publishes continuously, and because the map is generated from the world
        # geometry the map frame equals the Gazebo world frame - so the two are directly
        # comparable with the item poses.
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose",
                                 self._on_amcl_pose, 10)
        self._amcl_position = None
        self._amcl_yaw = 0.0

        # One publisher pair per item; created lazily so adding items needs no code change.
        self._attach_pubs = {}
        self._detach_pubs = {}

        group = ReentrantCallbackGroup()
        self.create_service(Grab, "~/grab", self.on_grab, callback_group=group)
        self.create_service(Grab, "~/release", self.on_release, callback_group=group)

        # Start from a known state: empty-handed.
        self.create_timer(2.0, self._initial_release_once)
        self._did_initial_release = False

        self.get_logger().info(
            f"Gripper ready (hold_tolerance={self.hold_tolerance} m). "
            "Services: ~/grab and ~/release")

    # ---------------------------------------------------------------- helpers
    def _pubs_for(self, item):
        """Get (attach, detach) publishers for an item, creating them on first use."""
        if item not in self._attach_pubs:
            self._attach_pubs[item] = self.create_publisher(
                Empty, f"/gripper/{item}/attach", 10)
            self._detach_pubs[item] = self.create_publisher(
                Empty, f"/gripper/{item}/detach", 10)
            time.sleep(0.3)      # let the new publishers connect before first use
        return self._attach_pubs[item], self._detach_pubs[item]

    def _watch_model(self, name):
        """Subscribe to a model's world pose topic and cache the latest value."""
        if name in self._pose_subs:
            return
        self._pose_subs[name] = self.create_subscription(
            Pose, f"/model/{name}/pose",
            lambda msg, n=name: self.poses.__setitem__(
                n, (msg.position.x, msg.position.y, msg.position.z)),
            10)

    def _watch_state(self, name):
        """Track what the DetachableJoint says about this item: attached or detached."""
        if name in self._state_subs:
            return
        self._state_subs[name] = self.create_subscription(
            String, f"/gripper/{name}/state",
            lambda msg, n=name: self.attach_state.__setitem__(n, msg.data.strip()),
            10)

    def _is_held(self, item):
        """True/False from the joint itself, or None if it has never reported."""
        state = self.attach_state.get(item)
        return None if state is None else state == "attached"

    def _initial_release_once(self):
        if self._did_initial_release:
            return
        self._did_initial_release = True
        for item in self.items:
            _, detach = self._pubs_for(item)
            detach.publish(Empty())
        self.get_logger().info(
            f"Start-up: released all items {self.items} so none are welded to the gripper.")

    def _on_amcl_pose(self, msg):
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        self._amcl_position = (p.x, p.y)
        self._amcl_yaw = yaw

    def _robot_position(self):
        """Where the robot is, preferring AMCL over the Gazebo pose publisher."""
        if self._amcl_position is not None:
            return self._amcl_position
        return self.poses.get(self.robot_model)

    def _snap_to_gripper(self, item):
        """Teleport `item` to the gripper position via Gazebo's set_pose service."""
        robot = self._robot_position()
        if robot is None:
            return False
        x = robot[0] + self.carry_offset * math.cos(self._amcl_yaw)
        y = robot[1] + self.carry_offset * math.sin(self._amcl_yaw)
        req = (f'name: "{item}", position: {{x: {x:.4f}, y: {y:.4f}, z: 0.05}}, '
               f'orientation: {{w: 1.0}}')
        try:
            subprocess.run(
                ["gz", "service", "-s", f"/world/{self.world}/set_pose",
                 "--reqtype", "gz.msgs.Pose", "--reptype", "gz.msgs.Boolean",
                 "--timeout", "2000", "--req", req],
                capture_output=True, timeout=6, check=False)
            time.sleep(0.4)          # let the physics engine settle the new pose
            self.get_logger().info(f"Snapped {item} to the gripper at "
                                   f"({x:.2f}, {y:.2f}).")
            return True
        except Exception as exc:                                  # noqa: BLE001
            self.get_logger().warn(f"Could not snap {item} to the gripper: {exc}")
            return False

    def _await_poses(self, item, timeout=3.0):
        """Wait briefly for the item's and robot's poses to arrive.

        Pose updates are event-driven, so a node that starts after a model has come to
        rest may not have seen it yet. Waiting avoids reporting a spurious failure.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if item in self.poses and self._robot_position() is not None:
                return True
            time.sleep(0.1)
        return item in self.poses and self._robot_position() is not None

    def _gap_to_gripper(self, item):
        """Planar distance between the item and the robot's gripper, or None if unknown.

        The gripper sits at the front of the chassis, so we compare against the robot
        origin plus that offset in the plane; exact orientation is not needed at this
        tolerance.
        """
        robot = self._robot_position()
        if item not in self.poses or robot is None:
            return None
        ix, iy, _ = self.poses[item]
        rx, ry = robot[0], robot[1]
        return math.hypot(ix - rx, iy - ry)

    # --------------------------------------------------------------- services
    def on_grab(self, request, response):
        item = request.item or "item_1"
        self._watch_model(item)
        self._watch_state(item)
        attach, _ = self._pubs_for(item)

        self._await_poses(item)
        gap_before = self._gap_to_gripper(item)
        if gap_before is None:
            response.success = False
            response.message = f"pose for '{item}' or the robot is unknown"
            response.distance = -1.0
            self.get_logger().warn(response.message)
            return response

        # Refuse to "grab" something that is nowhere near the gripper: this is the
        # failure mode the retry state machine is designed to recover from.
        if gap_before > self.hold_tolerance:
            response.success = False
            response.distance = gap_before
            response.message = (f"{item} is {gap_before:.2f} m away, "
                                f"further than the {self.hold_tolerance:.2f} m the gripper can reach")
            self.get_logger().warn(f"Grab failed: {response.message}")
            return response

        attach.publish(Empty())
        time.sleep(self.settle_time)

        gap_after = self._gap_to_gripper(item)
        held = self._is_held(item)

        if held is None:
            # The joint has never reported - fall back to proximity, but SAY SO. This is
            # the weak check: it cannot tell a real attach from a command that did nothing.
            ok = gap_after is not None and gap_after <= self.hold_tolerance
            evidence = "proximity only - the joint has not reported its state"
        else:
            ok = held
            evidence = f"joint reports '{self.attach_state[item]}'"

        response.success = ok
        response.distance = gap_after if gap_after is not None else -1.0
        gap_text = f"{gap_after:.2f} m" if gap_after is not None else "unknown"
        if ok:
            self.held_item = item
            response.message = f"grabbed {item} ({evidence}, gap {gap_text})"
            self.get_logger().info(response.message)
        else:
            response.message = (f"attach command sent but {item} is NOT held "
                                f"({evidence}, gap {gap_text})")
            self.get_logger().warn(f"Grab failed: {response.message}")
        return response

    def on_release(self, request, response):
        item = request.item or self.held_item or "item_1"
        self._watch_state(item)
        _, detach = self._pubs_for(item)

        detach.publish(Empty())
        time.sleep(self.settle_time)

        if self.held_item == item:
            self.held_item = None
        gap = self._gap_to_gripper(item)

        # A release that did not actually let go strands the parcel on the gripper and the
        # next delivery drags it around, so confirm rather than assume.
        still_held = self._is_held(item)
        response.success = still_held is not True
        response.distance = gap if gap is not None else -1.0
        if response.success:
            response.message = f"released {item}"
            self.get_logger().info(response.message)
        else:
            response.message = f"detach command sent but the joint still reports {item} attached"
            self.get_logger().warn(f"Release failed: {response.message}")
        return response


def main():
    rclpy.init()
    node = GripperNode()
    executor = rclpy.executors.MultiThreadedExecutor()
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
