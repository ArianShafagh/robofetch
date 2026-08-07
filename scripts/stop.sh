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
PATTERN='gz|ruby|rviz2|gripper_node|task_manager|robot_state|parameter_bridg|map_server|amcl|lifecycle_manag|controller_serv|smoother_server|planner_server|behavior_server|bt_navigator|waypoint_follow|velocity_smooth|collision_monit|opennav_docking|ros2'

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
