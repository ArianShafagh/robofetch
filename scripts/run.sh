#!/usr/bin/env bash
# RoboFetch cold start: clean up, build, source, launch.
#
#   ./scripts/run.sh                 # normal demo, Gazebo + RViz
#   ./scripts/run.sh --headless      # no GUI (faster, for testing)
#   ./scripts/run.sh --no-build      # skip colcon build if nothing changed
#   ./scripts/run.sh --retry-demo    # stage a grab failure to show the retry FSM
#
# Safe to run from any directory and in a brand-new terminal - it sources everything
# itself. No `source` needed beforehand.
# NOTE: no `set -u`; ROS setup.bash references unbound variables and would abort.
set -e
WS="$(cd "$(dirname "$0")/.." && pwd)"

BUILD=1
LAUNCH_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --headless)   LAUNCH_ARGS+=('rviz:=false' 'gz_extra:=-s --headless-rendering') ;;
    --no-build)   BUILD=0 ;;
    --retry-demo) LAUNCH_ARGS+=("orders:=item_1,0.0,0.0,-3.1,-2.2; item_3,2.75,-1.0,-2.6,-1.5") ;;
    *)            LAUNCH_ARGS+=("$arg") ;;
  esac
done

echo "[run] 1/4  stopping anything left over from a previous run ..."
bash "$WS/scripts/stop.sh" || true

# Refuse to launch into a port clash. If something still holds 8000, uvicorn dies with
# "[Errno 98] address already in use" buried in thousands of Nav2 lines, while curl keeps
# answering normally - from the OLD process, wired to a different database and no robot.
# Every order you submit then vanishes into it. Far better to stop here and say so.
if ss -lptn 'sport = :8000' 2>/dev/null | tail -n +2 | grep -q .; then
  echo
  echo "[run] ERROR: port 8000 is STILL in use after stop.sh:"
  ss -lptn 'sport = :8000' | tail -n +2
  echo
  echo "[run] The web API cannot bind, so orders you POST would go to that stale process"
  echo "[run] instead of to the robot. Kill the PID shown above and run this again."
  exit 1
fi

echo "[run] 2/4  sourcing ROS 2 Jazzy ..."
source /opt/ros/jazzy/setup.bash

if [ "$BUILD" -eq 1 ]; then
  echo "[run] 3/4  building the workspace ..."
  cd "$WS"
  colcon build --symlink-install
else
  echo "[run] 3/4  skipping build (--no-build)"
fi

echo "[run] 4/4  sourcing the workspace and launching ..."
source "$WS/install/setup.bash"

echo
echo "  Gazebo and RViz will open. The robot starts on the green delivery station."
echo "  Expect ~35 s before it first moves, then ~4 minutes for all three deliveries."
echo "  Press Ctrl+C to stop, then run ./scripts/stop.sh before launching again."
echo
exec ros2 launch robofetch_bringup delivery.launch.py "${LAUNCH_ARGS[@]}"
