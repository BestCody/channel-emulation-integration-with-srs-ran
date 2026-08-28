#!/usr/bin/env bash

python3 /srsran/config/generate_ue_conf.py $1 /tmp/
ip netns add ue$1
/opt/srsRAN_4G/build/srsue/src/srsue /tmp/ue_$1.conf
