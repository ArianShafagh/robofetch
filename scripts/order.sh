#!/usr/bin/env bash
# Submit a delivery order and FOLLOW it until it finishes.
#
#   ./scripts/order.sh                             # item_1, shelf_1 -> delivery_1
#   ./scripts/order.sh item_2 shelf_2 delivery_2
#
# Why this exists instead of a bare curl:
#
#   curl -X POST localhost:8000/orders ...
#
# returns a valid JSON order and then tells you NOTHING else. Every way this system can
# fail still returns that same JSON, so "the order was accepted" and "the order will never
# run" look identical from a second terminal. The three real causes are:
#
#   1. the API is not up yet            -> connection refused
#   2. a STALE api holds port 8000      -> answers you perfectly, wired to no robot
#   3. the robot is not localized yet   -> order sits at 'pending' forever
#
# This script checks all three BEFORE submitting, then prints every state change, and
# finally compares the parcel's REAL position in Gazebo against the drop-off - because a
# status of 'completed' is a claim, and the parcel's world pose is the evidence (HANDOVER
# 5.4).
#
# NOTE: no `set -u`; ROS setup.bash references unbound variables and would abort.
set -e
WS="$(cd "$(dirname "$0")/.." && pwd)"
API="${ROBOFETCH_API:-localhost:8000}"

ITEM="${1:-item_1}"
POINT_A="${2:-shelf_1}"
POINT_B="${3:-delivery_1}"

py() { python3 -c "$@"; }

# ---------------------------------------------------------------- pre-flight checks
echo "[order] checking the API on $API ..."
HEALTH="$(curl -sf "http://$API/health" 2>/dev/null || true)"
if [ -z "$HEALTH" ]; then
  echo "[order] ERROR: nothing is answering on $API."
  echo "[order]   The stack is probably still starting - the API comes up ~8 s in, and"
  echo "[order]   the robot only starts accepting orders once you see this in the launch"
  echo "[order]   terminal:  'Localized and ready - orders submitted now will execute.'"
  echo "[order]   If the launch has finished starting, check it for 'address already in use'."
  exit 1
fi

CONNECTED="$(printf '%s' "$HEALTH" | py 'import sys,json; print(json.load(sys.stdin).get("robot_connected"))')"
DB="$(printf '%s' "$HEALTH" | py 'import sys,json; print(json.load(sys.stdin).get("database"))')"
echo "[order]   database        : $DB"
echo "[order]   robot connected : $CONNECTED"

if [ "$CONNECTED" != "True" ]; then
  echo
  echo "[order] ERROR: the API is running but NO ROBOT is subscribed to /orders/new."
  echo "[order]   An order submitted now would be stored and never executed."
  echo "[order]   Most likely this is a STALE api left over from an earlier run: it holds"
  echo "[order]   port 8000, so the real launch's API died with 'address already in use'."
  echo "[order]   Check the database path above - if it is not ~/robofetch_ws/robofetch.db"
  echo "[order]   you are talking to the wrong process. Fix with:"
  echo
  echo "      ./scripts/stop.sh && ./scripts/run.sh --headless"
  echo
  exit 1
fi

# ---------------------------------------------------------------------- submit
echo "[order] submitting: $ITEM  $POINT_A -> $POINT_B"
RESPONSE="$(curl -sf -X POST "http://$API/orders" \
  -H 'Content-Type: application/json' \
  -d "{\"item\":\"$ITEM\",\"point_a\":\"$POINT_A\",\"point_b\":\"$POINT_B\"}" 2>/dev/null || true)"

if [ -z "$RESPONSE" ]; then
  echo "[order] ERROR: the API rejected the order. Unknown item or waypoint name?"
  echo "[order]   Known waypoints:"
  curl -sf "http://$API/locations" | py 'import sys,json
for l in json.load(sys.stdin): print(f"     {l[\"name\"]:<12} ({l[\"x\"]:+.2f}, {l[\"y\"]:+.2f})")'
  exit 1
fi

ID="$(printf '%s' "$RESPONSE" | py 'import sys,json; print(json.load(sys.stdin)["id"])')"
echo "[order] accepted as order $ID"
echo

# ------------------------------------------------------------------- follow it
# Poll rather than use the WebSocket so this stays dependency-free.
LAST=""
PENDING_FOR=0
for _ in $(seq 1 240); do
  ORDER="$(curl -sf "http://$API/orders/$ID" 2>/dev/null || true)"
  [ -z "$ORDER" ] && { echo "[order] lost contact with the API."; exit 1; }

  STATUS="$(printf '%s' "$ORDER" | py 'import sys,json; print(json.load(sys.stdin)["status"])')"
  DETAIL="$(printf '%s' "$ORDER" | py 'import sys,json; print(json.load(sys.stdin).get("detail") or "")')"

  if [ "$STATUS$DETAIL" != "$LAST" ]; then
    printf '  %-12s %s\n' "$STATUS" "$DETAIL"
    LAST="$STATUS$DETAIL"
  fi

  case "$STATUS" in
    completed|failed|cancelled) break ;;
    pending)
      PENDING_FOR=$((PENDING_FOR + 2))
      # 'pending' means the robot has not picked it up yet. Briefly that is normal; for a
      # long time it means the task manager is not localized and cannot start anything.
      if [ "$PENDING_FOR" = 40 ]; then
        echo "  ... still pending after 40 s. The robot is most likely not localized yet."
        echo "      Look in the launch terminal for 'accepted by AMCL'. Do NOT trust"
        echo "      'ros2 topic echo /amcl_pose' - it is latched and answers even when"
        echo "      AMCL has been silent for minutes (HANDOVER 4)."
      fi
      ;;
  esac
  sleep 2
done

echo
echo "[order] final status: $STATUS"
echo "[order] detail       : $DETAIL"

# --------------------------------------------------- verify against Gazebo ground truth
# The status above is what the software BELIEVES. This is what actually happened.
echo
echo "[order] verifying against Gazebo ground truth ..."
source /opt/ros/jazzy/setup.bash >/dev/null 2>&1 || true

POSE="$(gz topic -e -t /world/warehouse/dynamic_pose/info -n 1 2>/dev/null \
        | grep -A6 "name: \"$ITEM\"" | grep -E '^\s+(x|y):' | head -2 || true)"

if [ -z "$POSE" ]; then
  echo "[order]   could not read $ITEM's pose from Gazebo (is the sim running?)"
  exit 0
fi

IX="$(printf '%s' "$POSE" | awk '/x:/ {print $2; exit}')"
IY="$(printf '%s' "$POSE" | awk '/y:/ {print $2; exit}')"
TARGET="$(curl -sf "http://$API/locations" | py "import sys,json
for l in json.load(sys.stdin):
    if l['name'] == '$POINT_B': print(l['x'], l['y'])")"

py "import math
ix, iy = $IX, $IY
tx, ty = [float(v) for v in '$TARGET'.split()]
gap = math.hypot(ix - tx, iy - ty)
print(f'[order]   {\"$ITEM\"} is really at ({ix:+.2f}, {iy:+.2f})')
print(f'[order]   {\"$POINT_B\"} is at        ({tx:+.2f}, {ty:+.2f})')
print(f'[order]   distance from target: {gap:.2f} m')
print()
if gap <= 1.1:
    print('[order]   VERIFIED: the parcel really was delivered.')
else:
    print('[order]   NOT DELIVERED: the parcel is not at the drop-off point.')
    print('[order]   If the status above says completed, the software and physical')
    print('[order]   reality disagree - that is the bug HANDOVER 5.4 is about.')"
