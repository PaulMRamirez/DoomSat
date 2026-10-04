#!/bin/bash
# The flight components' unit tests (GTest), in a unit-test build of the F´ project beside the flight build.
# Linux, macOS or inside WSL; on Windows call scripts/flight.sh ut, which forwards here.
# The first run configures the unit-test build (about a minute and a half); later runs take seconds.
set -e
. "$(dirname "$0")/common.sh"
bash "$DOOMSAT_REPO/scripts/wsl_sync.sh"
cd "$PROJ"
. fprime-venv/bin/activate
UT=build-fprime-automatic-native-ut
# A unit test registered after the unit-test build was configured is only picked up by configuring it again
if [ ! -f "$UT/CMakeCache.txt" ] || [ DoomMission/Components/CfdpGuard/CMakeLists.txt -nt "$UT/CMakeCache.txt" ]; then
  fprime-util generate --ut 2>&1 | grep -v "fprime-gds has unexpected" | tail -3
  touch "$UT/CMakeCache.txt"
fi
cd DoomMission/Components/CfdpGuard
fprime-util check -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" 2>&1 | grep -v "fprime-gds has unexpected"
exit "${PIPESTATUS[0]}"
