#!/bin/bash

pid=$(ps ax | grep '[/]srsran/gnb' | awk '{print $1}')

if [[ -z "$pid" ]]; then
    echo "Process /srsran/gnb not found."
    exit 1
fi

echo "PID of /srsran/gnb: $pid"

cgroup_info=$(cat /proc/$pid/cgroup | sed 's/^0:://')

if [[ -z "$cgroup_info" ]]; then
    echo "Failed to get cgroup information for PID $pid."
    exit 1
fi

CPU=$1
echo "${CPU}00 100000" | sudo tee /sys/fs/cgroup$cgroup_info/cpu.max > /dev/null
cat /sys/fs/cgroup/$cgroup_info/cpu.max
