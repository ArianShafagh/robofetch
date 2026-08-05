#!/usr/bin/env bash
# Launch the RoboFetch simulation (Gazebo GUI + robot + bridge + RViz).
#
# Usage:
#   ./scripts/sim.sh            # GUI, GPU rendering (d3d12) — fastest, try this first
#   ./scripts/sim.sh soft       # GUI, SOFTWARE rendering — use if the window is BLANK on WSL
#   ./scripts/sim.sh headless   # no GUI, no RViz (for automated testing)
set -e
WS="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$WS/install/setup.bash"

# --- WSLg display fixes ---
# Force Qt to use X11 (xcb) instead of Wayland: fixes many blank/black gz-gui windows.
export QT_QPA_PLATFORM=xcb

case "$1" in
  headless)
    exec ros2 launch robofetch_gazebo sim.launch.py gz_extra:="-s --headless-rendering" rviz:=false
    ;;
  soft)
    # llvmpipe software rendering: slow but reliable when the GPU path renders blank.
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
    echo "[sim] SOFTWARE rendering (llvmpipe). Slower but should always display."
    exec ros2 launch robofetch_gazebo sim.launch.py
    ;;
  *)
    # GPU path via WSL's Direct3D12 Mesa driver (auto-selects your GPU).
    echo "[sim] GPU rendering (d3d12). If the window is BLANK, retry with:  ./scripts/sim.sh soft"
    exec ros2 launch robofetch_gazebo sim.launch.py
    ;;
esac
