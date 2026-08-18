"""Robot condition monitor (proposal §3.3 C1, FR10, FR12).

Integrates the coupled battery / temperature / wear model in `robot_model.py` against what the
robot is really doing, and publishes the result:

    subscribes /odom             - distance actually covered and current speed
    subscribes /robot/activity   - what the task manager is doing, and what it is carrying
    publishes  /robot/telemetry  - the condition, as JSON, at 1 Hz
    writes     logs/run_<id>.csv - one row per sample (FR12, the ML training input)

Distance comes from odometry rather than from the commanded velocity on purpose: if the robot
is stuck against a wall the wheels turn but the robot does not move, and a model driven by
commands would happily report energy being spent on travel that never happened.

The CSV schema here is the contract with tools/ml/. Change one and you must change the other,
or the model will be trained on columns the live system does not produce.
"""
import csv
import json
import math
import os
import time

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from std_msgs.msg import String

from robofetch_core.robot_model import (RobotCondition, T_MAX, T_RESUME, T_WARN,
                                        CONDITION_MIN, RESERVE_PERCENT)

CSV_FIELDS = ["ts", "run_id", "order_id", "activity", "state", "battery_percent",
              "temperature_c", "condition_percent", "payload_kg", "motor_load", "speed",
              "distance_delta_m", "cumulative_distance_m", "cumulative_energy_wh"]


class RobotStateNode(Node):
    def __init__(self):
        super().__init__("robot_state_node")

        self.declare_parameter("publish_period", 1.0)
        # Where per-run CSV logs go. One file per launch, named with the run id.
        self.declare_parameter("log_dir", os.path.expanduser("~/robofetch_ws/logs"))
        # Starting condition. Defaults to a healthy, fully charged robot; override to stage
        # a demo where the robot has to refuse work.
        self.declare_parameter("initial_battery", 100.0)
        self.declare_parameter("initial_condition", 100.0)
        # Speed the drive is considered "at full load", for scaling the thermal model.
        self.declare_parameter("nominal_speed", 0.22)

        self.period = self.get_parameter("publish_period").value
        self.nominal_speed = self.get_parameter("nominal_speed").value

        self.condition = RobotCondition(
            battery_percent=float(self.get_parameter("initial_battery").value),
            condition_percent=float(self.get_parameter("initial_condition").value))

        self.run_id = time.strftime("run_%Y%m%d_%H%M%S")
        self.activity = "idle"          # what the task manager says it is doing
        self.payload_kg = 0.0
        self.order_id = None

        self._last_xy = None
        self._pending_distance = 0.0    # metres since the last telemetry tick
        self._speed = 0.0
        self._last_tick = time.time()

        self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        self.create_subscription(String, "/robot/activity", self._on_activity, 10)
        self.telemetry_pub = self.create_publisher(String, "/robot/telemetry", 10)

        log_dir = self.get_parameter("log_dir").value
        os.makedirs(log_dir, exist_ok=True)
        self.csv_path = os.path.join(log_dir, f"{self.run_id}.csv")
        self._csv = open(self.csv_path, "w", newline="")
        self._writer = csv.DictWriter(self._csv, fieldnames=CSV_FIELDS)
        self._writer.writeheader()

        self.create_timer(self.period, self._tick)
        self.get_logger().info(
            f"Robot state monitor up. Run {self.run_id}, logging to {self.csv_path}")

    # ------------------------------------------------------------------ incoming
    def _on_odom(self, msg):
        """Accumulate REAL distance travelled between telemetry ticks."""
        p = msg.pose.pose.position
        if self._last_xy is not None:
            self._pending_distance += math.hypot(p.x - self._last_xy[0],
                                                 p.y - self._last_xy[1])
        self._last_xy = (p.x, p.y)
        v = msg.twist.twist.linear
        self._speed = math.hypot(v.x, v.y)

    def _on_activity(self, msg):
        """The task manager tells us what it is doing and what it is carrying."""
        try:
            data = json.loads(msg.data)
        except ValueError:
            return
        self.activity = data.get("activity", self.activity)
        self.payload_kg = float(data.get("payload_kg", 0.0) or 0.0)
        self.order_id = data.get("order_id")

    # -------------------------------------------------------------------- model
    def _duty_state(self):
        """Where the robot sits in the duty cycle - the C1 state machine.

        Health states outrank activity: an overheated robot is in COOLDOWN whatever the task
        manager believes it is doing, and that is what admission control reads.
        """
        # A halted robot is halted, whatever else is true of it. Reporting "cooldown" for a robot
        # someone has just emergency-stopped would hide the one fact the operator needs.
        if self.activity == "stopped":
            return "stopped"
        if self.condition.temperature_c >= T_MAX:
            return "cooldown"
        if self.condition.condition_percent < CONDITION_MIN:
            return "fault"
        if self.activity == "charging":
            return "charging"
        if self.condition.temperature_c > T_RESUME and self.activity == "idle" \
                and self.condition.temperature_c > T_WARN:
            return "cooldown"
        return self.activity

    def _tick(self):
        now = time.time()
        dt = max(1e-3, now - self._last_tick)
        self._last_tick = now

        distance = self._pending_distance
        self._pending_distance = 0.0

        docked = self.activity == "charging"
        # Motor load tracks how hard the drive is working right now, saturated at 1.
        load = 0.0 if docked else min(1.0, self._speed / max(1e-3, self.nominal_speed))

        self.condition.step(dt, distance_m=distance, payload_kg=self.payload_kg,
                            motor_load=load, docked=docked)

        state = self._duty_state()
        sample = {
            "ts": now,
            "run_id": self.run_id,
            "order_id": self.order_id,
            "activity": self.activity,
            "state": state,
            "battery_percent": round(self.condition.battery_percent, 3),
            "temperature_c": round(self.condition.temperature_c, 3),
            "condition_percent": round(self.condition.condition_percent, 3),
            "payload_kg": self.payload_kg,
            "motor_load": round(load, 3),
            "speed": round(self._speed, 3),
            "distance_delta_m": round(distance, 4),
            "cumulative_distance_m": round(self.condition.cumulative_distance_m, 3),
            "cumulative_energy_wh": round(self.condition.cumulative_energy_wh, 4),
        }

        self._writer.writerow(sample)
        self._csv.flush()          # flush every tick: a killed launch must not lose the run
        self.telemetry_pub.publish(String(data=json.dumps(sample)))

        # Warn once per crossing rather than every tick.
        if state == "cooldown" and self.condition.temperature_c >= T_MAX:
            self.get_logger().warn(
                f"Motors at {self.condition.temperature_c:.0f} C - cooling down.")
        if self.condition.battery_percent < RESERVE_PERCENT:
            self.get_logger().warn(
                f"Battery {self.condition.battery_percent:.0f}% - below reserve.")

    def destroy_node(self):
        try:
            self._csv.close()
        except Exception:                                          # noqa: BLE001
            pass
        super().destroy_node()


def main():
    rclpy.init()
    node = RobotStateNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
