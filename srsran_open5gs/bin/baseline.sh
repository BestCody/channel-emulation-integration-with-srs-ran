#!/usr/bin/env bash

set -Eeuo pipefail

: "${NAMESPACE:?NAMESPACE is required}"
: "${UE_NUMBER:?UE_NUMBER is required}"
: "${GNB_SELECTOR:?GNB_SELECTOR is required}"
: "${UE_SELECTOR:?UE_SELECTOR is required}"
: "${GNB_CONTAINER:?GNB_CONTAINER is required}"
: "${UE_CONTAINER:?UE_CONTAINER is required}"
: "${UE_NETNS:?UE_NETNS is required}"
: "${GATEWAY:?GATEWAY is required}"
: "${TUN_INTERFACE:?TUN_INTERFACE is required}"
: "${START_GNU_SCRIPT:?START_GNU_SCRIPT is required}"
: "${START_GNB_SCRIPT:?START_GNB_SCRIPT is required}"
: "${START_UE_SCRIPT:?START_UE_SCRIPT is required}"
: "${FLOWGRAPH_PROCESS_PATTERN:?FLOWGRAPH_PROCESS_PATTERN is required}"
: "${UE_PROCESS_PATTERN:?UE_PROCESS_PATTERN is required}"
: "${GNB_PROCESS_PATTERN:?GNB_PROCESS_PATTERN is required}"
: "${ATTACHMENT_LOG_PHRASE:?ATTACHMENT_LOG_PHRASE is required}"

WAIT_SECONDS="${WAIT_SECONDS:-90}"
GNURADIO_LOG="${GNURADIO_LOG:?GNURADIO_LOG is required}"
GNB_LOG="${GNB_LOG:?GNB_LOG is required}"
UE_LOG="${UE_LOG:?UE_LOG is required}"

usage() {
  cat <<EOF
Usage: $0 {start|status|logs|stop}

Required environment variables are provided by the benchmark runner. For manual
use, set NAMESPACE, selectors, container names, UE_NETNS, GATEWAY, script paths,
log paths, and process patterns before calling this script.
EOF
}

get_pod() {
  local selector="$1"
  kubectl get pods -n "$NAMESPACE" -l "$selector"     --field-selector=status.phase=Running     -o jsonpath='{.items[0].metadata.name}'
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

start_baseline() {
  resolve_pods

  start_component     "GNU Radio"     "pgrep -f '$FLOWGRAPH_PROCESS_PATTERN' >/dev/null"     "nohup $START_GNU_SCRIPT $UE_NUMBER >'$GNURADIO_LOG' 2>&1 </dev/null &"     exec_ue

  sleep 2

  start_component     "gNB"     "pgrep -f '$GNB_PROCESS_PATTERN' >/dev/null"     "nohup $START_GNB_SCRIPT >'$GNB_LOG' 2>&1 </dev/null &"     exec_gnb

  sleep 2

  start_component     "UE ${UE_NUMBER}"     "pgrep -f '$UE_PROCESS_PATTERN' >/dev/null"     "nohup $START_UE_SCRIPT $UE_NUMBER >'$UE_LOG' 2>&1 </dev/null &"     exec_ue

  echo "Waiting for UE ${UE_NUMBER} to establish a PDU session..."
  for ((second = 1; second <= WAIT_SECONDS; second++)); do
    if exec_ue "grep -Fq '$ATTACHMENT_LOG_PHRASE' '$UE_LOG'" >/dev/null 2>&1; then
      exec_ue "ip netns exec '$UE_NETNS' ip route replace default via '$GATEWAY'"
      echo "Baseline ready. UE ${UE_NUMBER} is attached and its default route is set."
      status_baseline
      return
    fi

    if ! exec_ue "pgrep -f '$UE_PROCESS_PATTERN' >/dev/null" >/dev/null 2>&1; then
      echo "The UE process stopped before attachment completed." >&2
      exec_ue "tail -n 40 '$UE_LOG'" || true
      exit 1
    fi

    sleep 1
  done

  echo "Timed out after ${WAIT_SECONDS}s waiting for UE attachment." >&2
  echo "Run '$0 logs' to inspect the component logs." >&2
  exit 1
}

status_baseline() {
  resolve_pods

  printf "%-12s %s
" "Component" "Status"
  printf "%-12s %s
" "GNU Radio"     "$(exec_ue "pgrep -f '$FLOWGRAPH_PROCESS_PATTERN' >/dev/null && echo running || echo stopped")"
  printf "%-12s %s
" "gNB"     "$(exec_gnb "pgrep -f '$GNB_PROCESS_PATTERN' >/dev/null && echo running || echo stopped")"
  printf "%-12s %s
" "UE ${UE_NUMBER}"     "$(exec_ue "pgrep -f '$UE_PROCESS_PATTERN' >/dev/null && echo running || echo stopped")"

  echo
  exec_ue "ip netns exec '$UE_NETNS' ip -br addr show '$TUN_INTERFACE' 2>/dev/null || true"
  exec_ue "ip netns exec '$UE_NETNS' ip route 2>/dev/null || true"
}

show_logs() {
  resolve_pods

  echo "===== GNU Radio ====="
  exec_ue "tail -n 25 '$GNURADIO_LOG' 2>/dev/null || echo 'No GNU Radio log yet.'"
  echo
  echo "===== gNB ====="
  exec_gnb "tail -n 40 '$GNB_LOG' 2>/dev/null || echo 'No gNB log yet.'"
  echo
  echo "===== UE ${UE_NUMBER} ====="
  exec_ue "tail -n 40 '$UE_LOG' 2>/dev/null || echo 'No UE log yet.'"
}

stop_baseline() {
  resolve_pods

  exec_ue "pkill -INT -f '$UE_PROCESS_PATTERN' 2>/dev/null || true"
  sleep 2
  exec_gnb "pkill -INT -f '$GNB_PROCESS_PATTERN' 2>/dev/null || true"
  sleep 2
  exec_ue "pkill -INT -f '$FLOWGRAPH_PROCESS_PATTERN' 2>/dev/null || true"

  echo "Stopped UE ${UE_NUMBER}, gNB, and GNU Radio."
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
