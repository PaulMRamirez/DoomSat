#!/bin/bash
# Start (or restart) the flight side: Doom payload + Yamcs (fprime-yamcs) + the DoomSat binary.
# Linux, macOS or inside WSL; on Windows call scripts/flight.sh, which forwards here.
# Usage: wsl_run_flight.sh [start|yamcs|stop|status|payload]
#   start    payload + F´ + Yamcs
#   yamcs    F´ + Yamcs only, no game (for Yamcs / Open MCT on their own; the Doom channels stay still)
#   payload  restart only the game process (after editing the payload)
. "$(dirname "$0")/common.sh"
REPO=$DOOMSAT_REPO
# GEOMETRY=on   exact lines, gated on the automap having drawn them (payload/seen_geometry.py)
# ORACLE=L0|L1  the diagnostic ladder. Never on a shareware level, and never scored.
# WAD=, MAP=    which level. A dev flight is WAD=freedoom1.wad.
# DOOMSAT_RELAY=1  Yamcs's TM/TC links behind tools/lossy_relay.py (ground/yamcs/launch.py); off by default.
mkdir -p "$RUN" "$REPO/out" "$WADS/uplink"   # uplink: where an uplinked WAD lands (LOAD_WAD, README)
stop() {
  pkill -f "doom_payloa[d].py --fps" 2>/dev/null
  pkill -f "fprime_yamc[s]" 2>/dev/null; pkill -f "yamcs/launc[h].py" 2>/dev/null
  pkill -f "YamcsServe[r]" 2>/dev/null
  pkill -f "bin/DoomSa[t]" 2>/dev/null
  pkill -f "fprime-gd[s] " 2>/dev/null; pkill -f "fprime_gds[.]executables" 2>/dev/null   # flight.sh gds
  sleep 1
  kill_hung_payload
  # Yamcs takes up to ~15 s to shut down; a new one started sooner fails, and the flight software with it
  for _ in $(seq 1 40); do pgrep -f "YamcsServe[r]" >/dev/null || break; sleep 0.5; done
}
# The payload closes its game on SIGTERM, but one stuck inside ViZDoom never gets to run that handler (the
# engine holds the GIL): kill it outright rather than leave it holding port 4242.
kill_hung_payload() { pkill -KILL -f "doom_payloa[d].py --fps" 2>/dev/null; }
# Detached, so it outlives this shell. setsid -f in WSL: anything started with plain nohup inside a
# `wsl bash -c` call dies when that call returns. macOS has no setsid; nohup is enough there.
detach() {
  if command -v setsid >/dev/null; then setsid -f bash -c "$1" < /dev/null
  else nohup bash -c "$1" < /dev/null > /dev/null 2>&1 & fi
}
start_payload() {
  # --skill must match research/levels.yaml run.skill, or the bench and the flight stack are playing
  # different games and their numbers cannot be compared. tests/test_runner.py pins the two together.
  cd "$PROJ" || exit 1
  detach "'$PAYLOAD_PY' '$REPO'/payload/doom_payload.py --fps ${FPS:-10} --quality ${QUALITY:-45} --skill ${SKILL:-3} --wad ${WAD:-doom1.wad} --map ${MAP:-E1M1} --geometry ${GEOMETRY:-off} --oracle ${ORACLE:-off} --map-png '$REPO/out/payload_map.png' > '$RUN/payload.log' 2>&1"
}
start_yamcs() {
  cd "$PROJ" || exit 1
  # Parameters the flight software loads at boot (prmDb reads PrmDb.dat from the binary's directory). Built from
  # flight/config/PrmDb.json at every start, so the repo is what holds; a PRM_SAVE on board lasts until then.
  # With a second file in bin/, the launchers can no longer guess the app: both are given --app.
  # @UPLINK@ is this machine's uplink directory: cfdpManager's temp and fail directories live under it, not in /tmp.
  mkdir -p "$WADS/uplink/.cfdp-tmp" "$WADS/uplink/.cfdp-fail"
  sed "s#@UPLINK@#$WADS/uplink#g" "$REPO/flight/config/PrmDb.json" > "$RUN/PrmDb.json"
  fprime-venv/bin/fprime-prm-write dat "$RUN/PrmDb.json" -d "$DEPLOY/dict/DoomSatTopologyDictionary.json" \
    -o "$DEPLOY/bin/PrmDb.dat" > "$RUN/prmdb.log" 2>&1 || echo "PrmDb.dat not built (see $RUN/prmdb.log); flying on parameter defaults"
  detach "cd '$PROJ' && . fprime-venv/bin/activate && export FPRIME_DOWNLINK_DIR='$RUN/downlink' && python '$REPO/ground/yamcs/launch.py' --deployment $DEPLOY --app $DEPLOY/bin/DoomSat --skip-browser-open --yamcs-config-dir '$REPO/ground/yamcs' --yamcs-data-dir '$RUN/yamcs-data' --yamcs-realtime-only-channels DoomSat.doom.FRAME_CHUNK > '$RUN/yamcs.log' 2>&1"
}
case "${1:-start}" in
  stop) stop; echo stopped ;;
  payload)
    pkill -f "doom_payloa[d].py --fps" 2>/dev/null; sleep 1; kill_hung_payload
    start_payload; echo "payload restarted" ;;
  status)
    ps aux | grep -E "doom_payloa[d]|fprime_yamc[s]|YamcsServe[r]|bin/DoomSa[t]" | awk '{print $11, $12, $13}' | sort | uniq -c
    curl -s http://localhost:8090/api/instances | grep -c '"name": "fprime-project"' ;;
  start) stop; start_payload; start_yamcs; echo "started; logs in $RUN; Yamcs on http://localhost:8090 in ~30 s" ;;
  yamcs) stop; start_yamcs; echo "F´ + Yamcs started (no game); logs in $RUN; Yamcs on http://localhost:8090 in ~30 s" ;;
  *) echo "usage: $0 [start|yamcs|stop|status|payload]"; exit 2 ;;
esac
