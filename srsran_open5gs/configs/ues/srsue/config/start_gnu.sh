#!/usr/bin/env bash

set -Eeuo pipefail

exec python3 -u /srsran/config/multi_ue_scenario.py -n "$1"
