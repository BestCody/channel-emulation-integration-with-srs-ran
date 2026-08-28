#!/usr/bin/env bash

set -Eeuo pipefail

UE_NUMBER="${1:?Usage: start_ue.sh UE_NUMBER}"
NETNS="ue${UE_NUMBER}"
CONFIG="/tmp/ue_${UE_NUMBER}.conf"

python3 /srsran/config/generate_ue_conf.py \
  "$UE_NUMBER" /tmp

if ip netns list | awk '{print $1}' | grep -Fxq "$NETNS"; then
  ip netns delete "$NETNS"
fi
ip netns add "$NETNS"

exec /opt/srsRAN_4G/build/srsue/src/srsue "$CONFIG"
