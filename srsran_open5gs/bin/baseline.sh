#!/usr/bin/env bash

set -Eeuo pipefail

: "${NAMESPACE:?NAMESPACE is required}"
: "${UE_NUMBER:?UE_NUMBER is required}"
: "${GNB_SELECTOR:?GNB_SELECTOR is required}"
: "${UE_SELECTOR:?UE_SELECTOR is required}"
: "${GNB_CONTAINER:?GNB_CONTAINER is required}"
: "${UE_CONTAINER:?UE_CONTAINER is required}"
: "${GATEWAY:?GATEWAY is required}"
: "${TUN_INTERFACE:?TUN_INTERFACE is required}"
: "${START_GNU_SCRIPT:?START_GNU_SCRIPT is required}"
: "${START_GNB_SCRIPT:?START_GNB_SCRIPT is required}"
: "${START_UE_SCRIPT:?START_UE_SCRIPT is required}"
: "${FLOWGRAPH_PROCESS_PATTERN:?FLOWGRAPH_PROCESS_PATTERN is required}"
: "${UE_PROCESS_PATTERN:?UE_PROCESS_PATTERN is required}"
: "${GNB_PROCESS_PATTERN:?GNB_PROCESS_PATTERN is required}"
: "${ATTACHMENT_LOG_PHRASE:?ATTACHMENT_LOG_PHRASE is required}"
: "${GNB_READY_LOG_PHRASE:?GNB_READY_LOG_PHRASE is required}"
: "${GNURADIO_READY_LOG_PHRASE:?GNURADIO_READY_LOG_PHRASE is required}"
: "${UE_READY_LOG_PHRASE:?UE_READY_LOG_PHRASE is required}"

: "${WAIT_SECONDS:?WAIT_SECONDS is required}"
GNURADIO_LOG="${GNURADIO_LOG:?GNURADIO_LOG is required}"
GNB_LOG="${GNB_LOG:?GNB_LOG is required}"
GNB_SCHEDULER_LOG="${GNB_SCHEDULER_LOG:?GNB_SCHEDULER_LOG is required}"
UE_LOG="${UE_LOG:?UE_LOG is required}"

usage() {
  cat <<EOF
Usage: $0 {start|status|logs|stop}

Required environment variables are provided by the benchmark runner. For manual
use, set NAMESPACE, selectors, container names, GATEWAY, script paths,
log paths, and process patterns before calling this script.
EOF
}

get_pod() {
  local selector="$1"
  kubectl get pods -n "$NAMESPACE" -l "$selector" \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
}

resolve_pods() {
  GNB_POD="$(get_pod "$GNB_SELECTOR")"
  UE_POD="$(get_pod "$UE_SELECTOR")"

  if [[ -z "$GNB_POD" || -z "$UE_POD" ]]; then
    echo "The running gNB or UE pod could not be found in namespace $NAMESPACE." >&2
    exit 1
  fi
}

exec_gnb() {
  kubectl exec -n "$NAMESPACE" "$GNB_POD" -c "$GNB_CONTAINER" -- bash -lc "$1"
}

exec_ue() {
  kubectl exec -n "$NAMESPACE" "$UE_POD" -c "$UE_CONTAINER" -- bash -lc "$1"
}

start_component() {
  local name="$1"
  local check_command="$2"
  local start_command="$3"
  local target="$4"

  if "$target" "$check_command" >/dev/null 2>&1; then
    echo "$name is already running."
    return
  fi

  "$target" "$start_command"
  echo "Started $name."
}

ue_log_path() {
  local ue_index="$1"
  if [[ "$UE_NUMBER" -eq 1 ]]; then
    printf '%s' "$UE_LOG"
    return
  fi
  local root="${UE_LOG%.*}"
  local extension="${UE_LOG#"$root"}"
  printf '%s-ue%s%s' "$root" "$ue_index" "$extension"
}

ue_running() {
  local ue_index="$1"
  exec_ue "pgrep -af '$UE_PROCESS_PATTERN' | grep -F '/tmp/ue_${ue_index}.conf' >/dev/null"
}

launch_one_ue() {
  local ue_index="$1"
  local ue_log
  ue_log="$(ue_log_path "$ue_index")"
  if ue_running "$ue_index" >/dev/null 2>&1; then
    echo "UE ${ue_index} is already running."
  else
    exec_ue "nohup $START_UE_SCRIPT $ue_index >'$ue_log' 2>&1 </dev/null &"
    echo "Started UE ${ue_index}."
  fi
}

wait_one_ue() {
  local ue_index="$1"
  local ue_log
  ue_log="$(ue_log_path "$ue_index")"
  echo "Waiting for UE ${ue_index} to establish a PDU session..."
  for ((second = 1; second <= WAIT_SECONDS; second++)); do
    if exec_ue "grep -Fq '$ATTACHMENT_LOG_PHRASE' '$ue_log'" >/dev/null 2>&1; then
      exec_ue "ip netns exec 'ue${ue_index}' ip route replace default via '$GATEWAY'"
      echo "UE ${ue_index} is attached."
      return
    fi
    if ! ue_running "$ue_index" >/dev/null 2>&1; then
      echo "UE ${ue_index} stopped before attachment." >&2
      exec_ue "tail -n 40 '$ue_log'" || true
      exit 1
    fi
    sleep 1
  done
  echo "UE ${ue_index} attachment timed out." >&2
  exit 1
}

wait_one_ue_ready() {
  local ue_index="$1"
  local ue_log
  ue_log="$(ue_log_path "$ue_index")"
  echo "Waiting for UE ${ue_index} radio readiness..."
  for ((second = 1; second <= WAIT_SECONDS; second++)); do
    if exec_ue "grep -Fq '$UE_READY_LOG_PHRASE' '$ue_log'" \
        >/dev/null 2>&1; then
      return
    fi
    if ! ue_running "$ue_index" >/dev/null 2>&1; then
      echo "UE ${ue_index} stopped before radio readiness." >&2
      exec_ue "tail -n 40 '$ue_log'" || true
      exit 1
    fi
    sleep 1
  done
  echo "UE ${ue_index} radio readiness timed out." >&2
  exit 1
}

wait_gnuradio_ready() {
  echo "Waiting for GNU Radio readiness..."
  for ((second = 1; second <= WAIT_SECONDS; second++)); do
    if exec_ue "grep -Fq '$GNURADIO_READY_LOG_PHRASE' '$GNURADIO_LOG'" \
        >/dev/null 2>&1; then
      return
    fi
    if ! exec_ue "pgrep -f '$FLOWGRAPH_PROCESS_PATTERN'" \
        >/dev/null 2>&1; then
      echo "GNU Radio stopped before readiness." >&2
      exit 1
    fi
    sleep 1
  done
  echo "GNU Radio readiness timed out." >&2
  exit 1
}

wait_gnb_ready() {
  echo "Waiting for gNB radio readiness..."
  for ((second = 1; second <= WAIT_SECONDS; second++)); do
    if exec_gnb "grep -Fq '$GNB_READY_LOG_PHRASE' '$GNB_SCHEDULER_LOG'" \
        >/dev/null 2>&1; then
      return
    fi
    if ! exec_gnb "pgrep -f '$GNB_PROCESS_PATTERN'" \
        >/dev/null 2>&1; then
      echo "gNB stopped before radio readiness." >&2
      exit 1
    fi
    sleep 1
  done
  echo "gNB radio readiness timed out." >&2
  exit 1
}

start_baseline() {
  resolve_pods

  start_component \
    "GNU Radio" \
    "pgrep -f '$FLOWGRAPH_PROCESS_PATTERN' >/dev/null" \
    "nohup $START_GNU_SCRIPT $UE_NUMBER >'$GNURADIO_LOG' 2>&1 </dev/null &" \
    exec_ue
  wait_gnuradio_ready

  for ((ue_index = 1; ue_index <= UE_NUMBER; ue_index++)); do
    launch_one_ue "$ue_index"
  done
  for ((ue_index = 1; ue_index <= UE_NUMBER; ue_index++)); do
    wait_one_ue_ready "$ue_index"
  done
  start_component \
    "gNB" \
    "pgrep -f '$GNB_PROCESS_PATTERN' >/dev/null" \
    "nohup $START_GNB_SCRIPT >'$GNB_LOG' 2>&1 </dev/null &" \
    exec_gnb
  wait_gnb_ready
  for ((ue_index = 1; ue_index <= UE_NUMBER; ue_index++)); do
    wait_one_ue "$ue_index"
  done
  echo "Baseline ready. All ${UE_NUMBER} UE(s) are attached."
  status_baseline
}

status_baseline() {
  resolve_pods

  printf "%-12s %s\n" "Component" "Status"
  printf "%-12s %s\n" "GNU Radio" \
    "$(exec_ue "pgrep -f '$FLOWGRAPH_PROCESS_PATTERN' >/dev/null && echo running || echo stopped")"
  printf "%-12s %s\n" "gNB" \
    "$(exec_gnb "pgrep -f '$GNB_PROCESS_PATTERN' >/dev/null && echo running || echo stopped")"
  for ((ue_index = 1; ue_index <= UE_NUMBER; ue_index++)); do
    printf "%-12s %s\n" "UE ${ue_index}" \
      "$(ue_running "$ue_index" >/dev/null 2>&1 && echo running || echo stopped)"
  done

  for ((ue_index = 1; ue_index <= UE_NUMBER; ue_index++)); do
    echo
    exec_ue "ip netns exec 'ue${ue_index}' ip -br addr show '$TUN_INTERFACE' 2>/dev/null || true"
    exec_ue "ip netns exec 'ue${ue_index}' ip route 2>/dev/null || true"
  done
}

show_logs() {
  resolve_pods

  echo "===== GNU Radio ====="
  exec_ue "tail -n 25 '$GNURADIO_LOG' 2>/dev/null || echo 'No GNU Radio log yet.'"
  echo
  echo "===== gNB ====="
  exec_gnb "tail -n 40 '$GNB_LOG' 2>/dev/null || echo 'No gNB log yet.'"
  echo
  for ((ue_index = 1; ue_index <= UE_NUMBER; ue_index++)); do
    echo "===== UE ${ue_index} ====="
    ue_log="$(ue_log_path "$ue_index")"
    exec_ue "tail -n 40 '$ue_log' 2>/dev/null || echo 'No UE log yet.'"
  done
}

stop_baseline() {
  resolve_pods

  exec_ue "pkill -INT -f '$UE_PROCESS_PATTERN' 2>/dev/null || true"
  sleep 2
  exec_gnb "pkill -INT -f '$GNB_PROCESS_PATTERN' 2>/dev/null || true"
  sleep 2
  exec_ue "pkill -INT -f '$FLOWGRAPH_PROCESS_PATTERN' 2>/dev/null || true"

  echo "Stopped ${UE_NUMBER} UE(s), gNB, and GNU Radio."
}

case "${1:-}" in
  start)
    start_baseline
    ;;
  status)
    status_baseline
    ;;
  logs)
    show_logs
    ;;
  stop)
    stop_baseline
    ;;
  *)
    usage
    exit 1
    ;;
esac
