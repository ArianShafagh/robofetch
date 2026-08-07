#!/usr/bin/env bash
# Order a product and follow it until it finishes. See scripts/order.py for the detail.
#
#   ./scripts/order.sh                        # SKU-1001 -> delivery_1
#   ./scripts/order.sh SKU-3001 delivery_2
#   ./scripts/order.sh --list                 # catalogue and destinations
#
# A thin wrapper: it sources ROS (so `gz topic` can read the ground-truth pose) and runs the
# real script on the venv interpreter. The logic lives in Python because building JSON inside
# nested shell quoting was more error-prone than the logic itself.
set -e
WS="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/jazzy/setup.bash >/dev/null 2>&1 || true
exec "$WS/robofetch_venv/bin/python" "$WS/scripts/order.py" "$@"
