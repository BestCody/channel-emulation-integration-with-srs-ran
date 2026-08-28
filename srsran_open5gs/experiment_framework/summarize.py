#!/usr/bin/env python3

import csv
import json
import pathlib
import re
import statistics

from .plots import dot_plot, line_plot
from .results import atomic_write_text, write_json


TRIAL_FIELDS = [
    "condition_id",
    "trial_number",
    "status",
    "attachment_success",
    "ue_ip",
    "packet_loss_percent",
    "rtt_mean_ms",
    "rtt_p95_ms",
    "connection_failures",
    "amf_restart_count_before",
    "amf_restart_count_after",
    "amf_memory_max_bytes",
]


def flatten_trial(summary):
    ping = summary.get("ping", {})
    rtt = ping.get("rtt_ms") or {}
    return {
        "condition_id": summary["condition_id"],
        "trial_number": summary["trial_number"],
        "status": summary.get("status"),
        "attachment_success": summary.get("attachment_success"),
        "ue_ip": summary.get("ue_ip"),
        "packet_loss_percent": ping.get("packet_loss_percent"),
        "rtt_mean_ms": rtt.get("mean"),
        "rtt_p95_ms": rtt.get("p95"),
        "connection_failures": summary.get("connection_failures", 0),
        "amf_restart_count_before": summary.get("amf", {}).get("restart_count_before"),
        "amf_restart_count_after": summary.get("amf", {}).get("restart_count_after"),
        "amf_memory_max_bytes": summary.get("amf", {}).get("memory_max_observed"),
    }


def write_csv(path, fieldnames, rows):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def aggregate(rows, key):
    values = [float(row[key]) for row in rows if row.get(key) not in (None, "")]
    if not values:
        return {"count": 0, "mean": None, "median": None, "minimum": None, "maximum": None, "sample_stddev": None}
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
        "sample_stddev": statistics.stdev(values) if len(values) > 1 else None,
    }


def numeric(value):
    match = re.search(r"-?[0-9.]+", str(value))
    return None if match is None else float(match.group())


def process_rows(trial_path, condition_id, trial_number):
    path = trial_path / "condition/monitoring/processes.jsonl"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            sample = json.loads(line)
        except json.JSONDecodeError:
            continue
        identity = sample.get("identity", {})
        expected = {
            str(identity.get("flowgraph_pid")): "gnuradio",
            str(identity.get("ue_pid")): "ue",
            str(identity.get("gnb_pid")): "gnb",
        }
        for field in ("ue_ps", "gnb_ps"):
            for process_line in sample.get(field, "").splitlines()[1:]:
                parts = process_line.split(None, 7)
                if len(parts) < 7 or parts[0] not in expected:
                    continue
                rows.append({
                    "condition_id": condition_id,
                    "trial_number": trial_number,
                    "time_ns": sample.get("time_ns"),
                    "component": expected[parts[0]],
                    "pid": int(parts[0]),
                    "cpu_percent": float(parts[2]),
                    "memory_percent": float(parts[3]),
                    "rss_kib": int(parts[4]),
                    "vsz_kib": int(parts[5]),
                })
    return rows


def gpu_rows(trial_path, condition_id, trial_number):
    path = trial_path / "condition/monitoring/gpu.csv"
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", newline="") as source:
        for raw in csv.DictReader(source):
            normalized = {key.strip(): value.strip() for key, value in raw.items()}
            rows.append({
                "condition_id": condition_id,
                "trial_number": trial_number,
                "timestamp": normalized.get("timestamp"),
                "gpu_index": int(numeric(normalized.get("index")) or 0),
                "gpu_uuid": normalized.get("uuid"),
                "gpu_utilization_percent": numeric(normalized.get("utilization.gpu [%]")),
                "memory_utilization_percent": numeric(normalized.get("utilization.memory [%]")),
                "memory_used_mib": numeric(normalized.get("memory.used [MiB]")),
                "power_w": numeric(normalized.get("power.draw [W]")),
                "temperature_c": numeric(normalized.get("temperature.gpu")),
            })
    return rows


def summarize_run(run_root):
    run_root = pathlib.Path(run_root)
    summaries = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((run_root / "trials").glob("*/trial-*/summary.json"))]
    rows = [flatten_trial(summary) for summary in summaries]
    summary_dir = run_root / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    (summary_dir / "plots").mkdir(parents=True, exist_ok=True)
    write_csv(summary_dir / "trials.csv", TRIAL_FIELDS, rows)
    grouped = {}
    for row in rows:
        grouped.setdefault(row["condition_id"], []).append(row)
    conditions = []
    for condition_id, condition_rows in grouped.items():
        conditions.append({
            "condition_id": condition_id,
            "trial_count": len(condition_rows),
            "successful_trials": sum(row["status"] == "passed" for row in condition_rows),
            "packet_loss_percent": aggregate(condition_rows, "packet_loss_percent"),
            "rtt_mean_ms": aggregate(condition_rows, "rtt_mean_ms"),
            "individual_trials_always_reported": True,
            "confidence_intervals": None,
            "confidence_interval_note": "Not reported; small trial counts do not support strong interval claims",
        })
    write_json(summary_dir / "conditions.json", conditions)

    moving_positions = []
    failures = []
    resources = []
    gpu_samples = []
    for trial_path in sorted((run_root / "trials").glob("*/trial-*")):
        condition_id = trial_path.parent.name
        trial_number = int(trial_path.name.split("-")[-1])
        result_path = trial_path / "condition/result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            moving = result.get("live")
            if isinstance(moving, dict) and isinstance(moving.get("records"), list):
                for record in moving["records"]:
                    moving_positions.append({
                        "condition_id": condition_id,
                        "trial_number": trial_number,
                        "position_index": record.get("index"),
                        "ue_index": record.get("ue_index"),
                        "alpha": record.get("alpha"),
                        "tap_count": record.get("tap_count"),
                    })
        failure_path = trial_path / "failure.json"
        if failure_path.exists():
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            failures.append(failure)
        resources.extend(process_rows(trial_path, condition_id, trial_number))
        gpu_samples.extend(gpu_rows(trial_path, condition_id, trial_number))

    amf_samples = []
    amf_path = run_root / "monitoring/amf-memory.jsonl"
    if amf_path.exists():
        for line in amf_path.read_text(encoding="utf-8").splitlines():
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "memory_current" in sample:
                amf_samples.append({
                    "time_ns": sample.get("time_ns"),
                    "pod": sample.get("pod"),
                    "pod_uid": sample.get("pod_uid"),
                    "container_id": sample.get("container_id"),
                    "restart_count": sample.get("restart_count"),
                    "memory_current": sample.get("memory_current"),
                    "memory_max": sample.get("memory_max"),
                    "limit_fraction": (
                        None if not sample.get("memory_max")
                        else sample["memory_current"] / sample["memory_max"]
                    ),
                })

    table_specs = [
        ("moving-positions.csv", moving_positions),
        ("failures.csv", failures),
        ("resource-samples.csv", resources),
        ("gpu-samples.csv", gpu_samples),
        ("amf-memory.csv", amf_samples),
    ]
    for filename, values in table_specs:
        fields = sorted({key for value in values for key in value})
        if not fields:
            fields = ["no_data"]
        write_csv(summary_dir / filename, fields, values)

    dot_plot(summary_dir / "plots/packet-loss.svg", "Packet loss by individual trial", rows, "packet_loss_percent", "Packet loss (%)")
    dot_plot(summary_dir / "plots/rtt-mean.svg", "Mean ping RTT by individual trial", rows, "rtt_mean_ms", "RTT (ms)")
    dot_plot(summary_dir / "plots/cpu.svg", "CPU by individual process sample", resources, "cpu_percent", "CPU (%)")
    dot_plot(summary_dir / "plots/gpu-utilization.svg", "GPU utilization samples", gpu_samples, "gpu_utilization_percent", "GPU utilization (%)")
    line_plot(summary_dir / "plots/amf-memory.svg", "AMF memory during pilot", amf_samples, "time_ns", "memory_current", "Time (ns)", "Memory (bytes)")
    atomic_write_text(
        summary_dir / "README.txt",
        "Individual trial results are shown in trials.csv.\n"
        "Confidence intervals are intentionally not reported for small trial counts.\n",
    )
    return {
        "trial_rows": rows,
        "conditions": conditions,
        "moving_positions": moving_positions,
        "failures": failures,
        "resources": resources,
        "gpu_samples": gpu_samples,
        "amf_samples": amf_samples,
    }
