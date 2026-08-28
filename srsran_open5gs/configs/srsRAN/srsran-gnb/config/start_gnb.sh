#!/usr/bin/env bash

set -Eeuo pipefail

python3 /srsran/config/render_gnb_config.py \
  /srsran/config/srsran-gnb.yaml /tmp/srsran-gnb.yaml
exec /srsran/gnb -c /tmp/srsran-gnb.yaml
