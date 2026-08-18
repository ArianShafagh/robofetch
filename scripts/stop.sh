#!/usr/bin/env bash
# Stop every RoboFetch / Gazebo / Nav2 process.
#
# Leftovers are genuinely harmful here, not just untidy: a second Gazebo keeps
# publishing /clock, which makes TF jump backwards and Nav2 abort every goal with
# confusing errors. Run this whenever a launch was killed uncleanly.
#
#   ./scripts/stop.sh
# NOTE: `ps -o comm` truncates to 15 characters, so these must be matched as PREFIXES of the
# truncated name, not as full executable names. `robot_state` deliberately covers BOTH
# `robot_state_publisher` -> "robot_state_pub" and `robot_state_node` -> "robot_state_nod".
# Listing only the former is how five orphaned condition monitors accumulated across restarts,
# each still publishing /robot/telemetry, so the web tier saw battery and temperature readings
# jumping between several stale robots at once.
#
# `route_server` was missing from this list until 2026-08-18 and cost hours. It is new in Nav2
# Jazzy, so it postdates the rest of the line. The symptom is vicious precisely because it does
# NOT look like a leftover-process problem: the old route_server keeps its node name on the
# network, the next run starts its own, and two nodes now answer to /route_server. The new
# lifecycle manager's transitions go astray, bringup ends with "Failed to bring up all requested
# nodes", and with no navigation stack there is no `map` frame at all - so RViz shows nothing and
# the robot never localizes. One survivor from a previous run breaks every run after it.
#
# If you add a Nav2 server, add it here. `--check` below exists to catch the omission.
PATTERN='gz|ruby|rviz2|gripper_node|task_manager|robot_state|parameter_bridg|map_server|amcl|lifecycle_manag|controller_serv|smoother_server|planner_server|behavior_server|bt_navigator|waypoint_follow|velocity_smooth|collision_monit|opennav_docking|route_server|ros2'

# BOTH web services run as `python -m uvicorn robofetch_<something>`, so their process NAME is
# just "python" - far too generic to put in PATTERN without killing unrelated work. They have
# to be matched on the full command line instead.
#
# Missing one is not cosmetic:
#   * a surviving BRIDGE keeps port 8000 bound, so the next run's API dies with
#     "[Errno 98] address already in use" while curl still answers happily - from the stale
#     process, against whatever database IT was started with;
#   * a surviving AI SERVICE keeps port 8001 bound the same way, so the next run's predictor
#     dies and every admission decision silently falls back to the policy-only path.
# In both cases the launch looks healthy and the fault appears to be somewhere else entirely.
#
# The pattern is deliberately `uvicorn robofetch_` rather than a list of service names, so a
# service added later is covered without anyone having to remember to update this line.
API_PATTERN='uvicorn robofetch_|robofetch_bridge\.app|robofetch_ai\.service'

pids_now() {
  # One snapshot, matched two ways. Taking it with command substitution means `ps` has
  # already exited before the greps run, so the greps cannot match themselves.
  local snapshot
  snapshot=$(ps -eo pid,comm,args --no-headers 2>/dev/null | awk -v me="$$" '$1 != me')
  {
    printf '%s\n' "$snapshot" | awk '{print $1" "$2}' \
      | grep -E "[[:space:]](${PATTERN})" | awk '{print $1}'
    printf '%s\n' "$snapshot" | grep -E "${API_PATTERN}" | awk '{print $1}'
  } | sort -u
}

# `./scripts/stop.sh --check` names every live ROS-looking process this script would NOT kill,
# without killing anything. Run it while the system is up: anything listed is a future leftover
# waiting to break the next launch, exactly as route_server did.
if [ "${1:-}" = "--check" ]; then
  missed=$(ps -eo pid,comm --no-headers | while read -r pid comm; do
    echo " $comm" | grep -qE "[[:space:]](${PATTERN})" && continue
    # Only ROS-shaped names; this box runs plenty of unrelated software.
    echo "$comm" | grep -qE "_server$|_node$|amcl|_monit|_follow|_smooth|_bridg|_docking|nav" \
      && echo "  $comm (pid $pid)"
  done | sort -u)
  if [ -z "$missed" ]; then
    echo "[stop] --check: every live ROS process is covered by PATTERN."
  else
    echo "[stop] --check: these would SURVIVE stop.sh and break the next launch:"
    echo "$missed"
    exit 1
  fi
  exit 0
fi

for signal in TERM TERM KILL; do
  pids=$(pids_now)
  [ -z "$pids" ] && break
  for pid in $pids; do kill -"$signal" "$pid" 2>/dev/null; done
  sleep 3
done

left=$(pids_now | wc -l)
if [ "$left" -eq 0 ]; then
  echo "[stop] all simulation processes stopped."
else
  echo "[stop] $left process(es) still running:"
  for pid in $(pids_now); do ps -o pid=,args= -p "$pid" 2>/dev/null; done
fi
