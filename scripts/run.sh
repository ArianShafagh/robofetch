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

# Refuse to launch into a port clash. If something still holds one of these ports, uvicorn
# dies with "[Errno 98] address already in use" buried in thousands of Nav2 lines while the
# port keeps answering normally - from the OLD process. On 8000 that means orders vanish into
# a stale API wired to a different database; on 8001 it means every admission decision quietly
# falls back to the policy-only path because the new predictor never started.
for PORT in 8000 8001; do
  if ss -lptn "sport = :$PORT" 2>/dev/null | tail -n +2 | grep -q .; then
    echo
    echo "[run] ERROR: port $PORT is STILL in use after stop.sh:"
    ss -lptn "sport = :$PORT" | tail -n +2
    echo
    echo "[run] That process would shadow the one this launch starts. Kill the PID above"
    echo "[run] and run this again."
    exit 1
  fi
done

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
