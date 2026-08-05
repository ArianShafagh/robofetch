#!/usr/bin/env bash
# Drive the robot with the keyboard (publishes to /cmd_vel).
# Run this in a SECOND terminal while sim.sh is running.
source /opt/ros/jazzy/setup.bash
exec ros2 run teleop_twist_keyboard teleop_twist_keyboard
