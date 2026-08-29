#!/usr/bin/env python3

import argparse
import json
import pathlib
import signal
import subprocess
import threading
import time


def kubectl(*arguments):
    return subprocess.check_output(
        ["kubectl", *arguments],
        text=True,
        timeout=10,
    ).strip()


def discover_amf(namespace, selector):
    return kubectl(
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        selector,
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    )


def read_sample(namespace, selector):
    pod = discover_amf(namespace, selector)
    metadata = json.loads(
        kubectl("get", "pod", pod, "-n", namespace, "-o", "json")
    )
    status = metadata["status"]["containerStatuses"][0]
    memory = kubectl(
        "exec",
        "-n",
        namespace,
        pod,
        "--",
        "sh",
        "-c",
        "printf '%s ' \"$(cat /sys/fs/cgroup/memory.current)\"; "
        "cat /sys/fs/cgroup/memory.max",
    ).split()
    maximum = None if memory[1] == "max" else int(memory[1])
    return {
        "time_ns": time.time_ns(),
        "pod": pod,
        "pod_uid": metadata["metadata"]["uid"],
        "container_id": status["containerID"],
        "restart_count": int(status["restartCount"]),
        "memory_current": int(memory[0]),
        "memory_max": maximum,
    }


def evaluate_sample(sample, baseline, *, stop_growth_bytes, warn_growth_bytes, stop_limit_fraction, warn_limit_fraction):
    reasons = []
    warnings = []
    if sample["restart_count"] != baseline["restart_count"]:
        reasons.append("AMF restart count changed")
    if sample["pod_uid"] != baseline["pod_uid"]:
        reasons.append("AMF pod UID changed")
    if sample["container_id"] != baseline["container_id"]:
        reasons.append("AMF container ID changed")

    growth = sample["memory_current"] - baseline["memory_current"]
    maximum = sample["memory_max"]
    if growth >= stop_growth_bytes:
        reasons.append(f"AMF memory grew by at least {stop_growth_bytes} bytes")
    elif growth >= warn_growth_bytes:
        warnings.append(f"AMF memory grew by at least {warn_growth_bytes} bytes")
    if maximum:
        fraction = sample["memory_current"] / maximum
        if fraction >= stop_limit_fraction:
            reasons.append(f"AMF memory reached {stop_limit_fraction:.0%} of its limit")
        elif fraction >= warn_limit_fraction:
            warnings.append(f"AMF memory reached {warn_limit_fraction:.0%} of its limit")
    return reasons, warnings


def append_json(path, value):
    with pathlib.Path(path).open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--selector", required=True)
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--stop-growth-bytes", type=int, required=True)
    parser.add_argument("--warn-growth-bytes", type=int, required=True)
    parser.add_argument("--stop-limit-fraction", type=float, required=True)
    parser.add_argument("--warn-limit-fraction", type=float, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()

    stop = threading.Event()

    def request_stop(sig=None, frame=None):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("", encoding="utf-8")

    baseline = read_sample(args.namespace, args.selector)
    baseline["event"] = "baseline"
    append_json(output, baseline)
    failures = 0
    final = {
        "status": "running",
        "baseline": baseline,
        "reason": None,
    }
    exit_code = 0
    while not stop.wait(args.interval):
        try:
            sample = read_sample(args.namespace, args.selector)
            failures = 0
            reasons, warnings = evaluate_sample(
                sample,
                baseline,
                stop_growth_bytes=args.stop_growth_bytes,
                warn_growth_bytes=args.warn_growth_bytes,
                stop_limit_fraction=args.stop_limit_fraction,
                warn_limit_fraction=args.warn_limit_fraction,
            )
            sample["warnings"] = warnings
            sample["stop_reasons"] = reasons
            append_json(output, sample)
            if reasons:
                final = {
                    "status": "unsafe",
                    "baseline": baseline,
                    "final": sample,
                    "reason": "; ".join(reasons),
                }
                exit_code = 2
                break
        except Exception as error:
            failures += 1
            append_json(
                output,
                {
                    "time_ns": time.time_ns(),
                    "sample_error": str(error),
                    "consecutive_failures": failures,
                },
            )
            if failures >= 3:
                final = {
                    "status": "unsafe",
                    "baseline": baseline,
                    "reason": "three consecutive AMF samples failed",
                }
                exit_code = 2
                break
    else:
        final = {
            "status": "stopped",
            "baseline": baseline,
            "reason": None,
        }

    pathlib.Path(args.summary).write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
