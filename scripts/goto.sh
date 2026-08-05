#!/usr/bin/env bash
# Send the robot to a named waypoint (or explicit coordinates) using Nav2.
#
#   ./scripts/goto.sh shelf1     -> in front of shelf 1  (-2.5,  0.95)
#   ./scripts/goto.sh shelf2     -> in front of shelf 2  ( 1.5,  0.95)
#   ./scripts/goto.sh shelf3     -> in front of shelf 3  ( 2.75, -1.0)
#   ./scripts/goto.sh delivery   -> the delivery station (-2.6, -2.0)
#   ./scripts/goto.sh 1.5 -2.0   -> arbitrary x y
#
# Because the map is generated from the world geometry, map coordinates ARE world
# coordinates, so these match the markers you see in Gazebo.
#
# Run `ros2 launch robofetch_nav navigation.launch.py` first, and set the robot's
# initial pose once (see ./scripts/set_pose.sh or RViz "2D Pose Estimate").
WS="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$WS/install/setup.bash"

case "${1:-}" in
  shelf1|s1)      X=-2.5; Y=0.95; NAME="shelf 1 (north-west)" ;;
  shelf2|s2)      X=1.5;  Y=0.95; NAME="shelf 2 (north-east)" ;;
  shelf3|s3)      X=2.75; Y=-1.0; NAME="shelf 3 (east)" ;;
  delivery|d)     X=-2.6; Y=-2.0; NAME="delivery station" ;;
  centre|center)  X=0.0;  Y=0.0;  NAME="centre of the room" ;;
  "")             echo "usage: $0 {shelf1|shelf2|shelf3|delivery|centre|<x> <y>}"; exit 1 ;;
  *)              X="$1"; Y="${2:?need a y coordinate}"; NAME="($X, $Y)" ;;
esac

echo "[goto] navigating to $NAME ..."
ros2 action send_goal /navigate_to_pose nav2_msgs/action/NavigateToPose \
  "{pose: {header: {frame_id: 'map'}, pose: {position: {x: $X, y: $Y, z: 0.0}, orientation: {w: 1.0}}}}"
