#!/usr/bin/env bash
# Grab or release the item with the simulated gripper.
#
#   ./scripts/gripper.sh grab              -> pick up item_1
#   ./scripts/gripper.sh release           -> put it down
#   ./scripts/gripper.sh grab item_2       -> operate on a different item
#
# Requires the simulation and `ros2 run robofetch_core gripper_node` to be running.
WS="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$WS/install/setup.bash"

ACTION="${1:-}"
ITEM="${2:-item_1}"

case "$ACTION" in
  grab)    SRV="/gripper_node/grab" ;;
  release) SRV="/gripper_node/release" ;;
  *) echo "usage: $0 {grab|release} [item_name]"; exit 1 ;;
esac

echo "[gripper] $ACTION $ITEM ..."
ros2 service call "$SRV" robofetch_interfaces/srv/Grab "{item: '$ITEM'}"
