#!/usr/bin/env bash

set -Eeuo pipefail

mkdir -p /dev/net
if [ ! -e /dev/net/tun ]; then
  mknod /dev/net/tun c 10 200
fi

while true; do
  sleep 3600
done
