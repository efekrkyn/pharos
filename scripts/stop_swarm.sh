#!/usr/bin/env bash
# Stop all PX4 SITL (SIH) swarm instances started by start_swarm.sh.
#
# Usage:
#   ~/drone-gcs/scripts/stop_swarm.sh
#
# Kills the 3 `px4 -i <N>` processes started by start_swarm.sh, plus any
# mavsdk_server processes the backend left behind bound to ports
# 14540/14541/14542. Uses precise patterns so it doesn't touch unrelated
# px4/mavsdk processes (e.g. a manually-run `make px4_sitl sihsim_quadx`
# for instance 0 uses the same binary but a different command line —
# matched here too since it's also part of the swarm's instance 0).

set -uo pipefail

echo "Stopping PX4 swarm instances..."
pkill -f "bin/px4 -i [0-2] " && echo "  killed 'bin/px4 -i N' instances" || echo "  no 'bin/px4 -i N' instances found"

echo "Stopping any mavsdk_server bound to swarm ports..."
for port in 14540 14541 14542; do
  pkill -f "mavsdk_server.*udp://:${port}" \
    && echo "  killed mavsdk_server on port $port" \
    || echo "  no mavsdk_server on port $port"
done

sleep 1

echo
echo "Remaining px4/mavsdk processes (should be empty or unrelated):"
pgrep -af "bin/px4|mavsdk_server" || echo "  (none)"
