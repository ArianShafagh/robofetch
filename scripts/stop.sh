#!/usr/bin/env bash
# Stop every RoboFetch / Gazebo / Nav2 process.
#
# Leftovers are genuinely harmful here, not just untidy: a second Gazebo keeps
# publishing /clock, which makes TF jump backwards and Nav2 abort every goal with
# confusing errors. Run this whenever a launch was killed uncleanly.
#
#   ./scripts/stop.sh
PATTERN='gz|ruby|rviz2|gripper_node|task_manager|robot_state_pub|parameter_bridg|map_server|amcl|lifecycle_manag|controller_serv|smoother_server|planner_server|behavior_server|bt_navigator|waypoint_follow|velocity_smooth|collision_monit|opennav_docking|ros2'

# The web API runs as `python -m uvicorn robofetch_bridge.app:app`, so its process NAME is
# just "python" - far too generic to put in PATTERN without killing unrelated work. It has
# to be matched on the full command line instead.
#
# Missing it is not cosmetic. A surviving API keeps port 8000 bound, so the NEXT run's API
# dies instantly with "[Errno 98] address already in use" while curl still answers happily
# - from the stale process, against whatever database IT was started with. Orders then get
# accepted and never execute, and every symptom points at the robot instead of at the port.
API_PATTERN='uvicorn robofetch_bridge|robofetch_bridge\.app'

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
