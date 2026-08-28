#!/usr/bin/env bash

set -Eeuo pipefail

NUM_UES="${1:?Usage: start_gnu_live_channel.sh NUM_UES}"
SAMPLE_RATE="$(
  python3 /srsran/config/fixed_channel.py \
    sample-rate /srsran/config/radio.json
)"

exec python3 -u /srsran/config/multi_ue_live_channel.py \
  --num-ues "$NUM_UES" \
  --sample-rate "$SAMPLE_RATE" \
  --control-bind tcp://0.0.0.0:5555
