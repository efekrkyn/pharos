#!/usr/bin/env bash
# Launch 3 headless PX4 SITL (SIH) instances for Pharos multi-drone testing.
#
# Usage:
#   ~/drone-gcs/scripts/start_swarm.sh
#
# Then, in another terminal, start the backend as usual:
#   cd ~/drone-gcs/backend && source ../.venv/bin/activate && uvicorn server:app --reload --port 8000
#
# Each instance is a separate PX4 process using the SIH (simulation-in-
# hardware) backend, started directly from the already-built
# px4_sitl_default binary (the same one `make px4_sitl sihsim_quadx` builds
# and runs for instance 0). Instance N listens for MAVSDK/MAVLink on UDP port
# 14540+N:
#   instance 0 -> udp://:14540, home (39.9228214, 32.8618589, 850)
#   instance 1 -> udp://:14541, home (39.9230214, 32.8618589, 850)
#   instance 2 -> udp://:14542, home (39.9226214, 32.8618589, 850)
#
# Logs go to ~/drone-gcs/scripts/logs/instance_N.{out,err}.log. Stop everything
# with stop_swarm.sh.

set -euo pipefail

PX4_DIR="${PX4_DIR:-$HOME/PX4-Autopilot}"
BUILD_DIR="$PX4_DIR/build/px4_sitl_default"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"

if [ ! -x "$BUILD_DIR/bin/px4" ]; then
  echo "error: $BUILD_DIR/bin/px4 not found." >&2
  echo "Build it first, e.g.: cd $PX4_DIR && make px4_sitl sihsim_quadx" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

export PX4_SIM_MODEL=sihsim_quadx

# Base home position: TED University, Ankara (matches the dashboard's
# default map view). Each instance gets a small latitude offset so the
# three vehicles don't spawn on top of each other.
BASE_LAT=39.9228214
BASE_LON=32.8618589
BASE_ALT=850

LAT_OFFSETS=(0.0 0.0002 -0.0002)

for n in 0 1 2; do
  working_dir="$BUILD_DIR/instance_$n"
  mkdir -p "$working_dir"

  export PX4_HOME_LAT=$(awk -v b="$BASE_LAT" -v o="${LAT_OFFSETS[$n]}" 'BEGIN { printf "%.7f", b + o }')
  export PX4_HOME_LON="$BASE_LON"
  export PX4_HOME_ALT="$BASE_ALT"

  echo "starting instance $n (home lat=$PX4_HOME_LAT, port=$((14540 + n))) in $working_dir"
  (
    cd "$working_dir"
    exec "$BUILD_DIR/bin/px4" -i "$n" -d "$BUILD_DIR/etc" \
      > "$LOG_DIR/instance_$n.out.log" 2> "$LOG_DIR/instance_$n.err.log"
  ) &
  disown
done

unset PX4_HOME_LAT PX4_HOME_LON PX4_HOME_ALT

sleep 3

echo
echo "PX4 instances running:"
pgrep -af "bin/px4 -i" || echo "  (none found — check $LOG_DIR/instance_*.err.log)"

echo
echo "Each instance sends MAVLink to udp://:1454<N> (N=0,1,2). The backend's"
echo "mavsdk_server processes bind those ports once you start uvicorn, so"
echo "there's nothing listening there until then — that's expected."
echo
echo "Logs: $LOG_DIR/instance_{0,1,2}.{out,err}.log"
echo "Stop with: $SCRIPT_DIR/stop_swarm.sh"
