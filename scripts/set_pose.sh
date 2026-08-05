#!/usr/bin/env bash
# Tell AMCL where the robot starts, so it can localize.
#
# The robot spawns on the delivery station (-2.6, -2.0) facing +x, so with no arguments this
# publishes exactly that. Equivalent to clicking "2D Pose Estimate" in RViz.
#
#   ./scripts/set_pose.sh            -> (-2.6, -2.0), facing +x
#   ./scripts/set_pose.sh 0.0 1.0    -> arbitrary x y
WS="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$WS/install/setup.bash"

X="${1:--2.6}"
Y="${2:--2.0}"

echo "[set_pose] telling AMCL the robot is at ($X, $Y) facing +x ..."
ros2 topic pub --once /initialpose geometry_msgs/msg/PoseWithCovarianceStamped \
  "{header: {frame_id: 'map'}, pose: {pose: {position: {x: $X, y: $Y, z: 0.0}, orientation: {w: 1.0}}}}"
echo "[set_pose] done. The AMCL particle cloud in RViz should collapse around the robot."
