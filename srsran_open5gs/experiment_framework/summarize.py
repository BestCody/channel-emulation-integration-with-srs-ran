#!/usr/bin/env python3

import csv
import json
import pathlib
import re
import statistics
import math

from .plots import dot_plot, line_plot
from .results import atomic_write_text, write_json


TRIAL_FIELDS = [
    "condition_id",
    "trial_number",
    "status",
    "link_status",
    "attachment_success",
    "ue_ips",
    "ping_transmitted",
    "ping_received",
    "packet_loss_percent",
    "rtt_mean_ms",
    "rtt_p95_ms",
    "uplink_mbps",
    "downlink_mbps",
    "uplink_failures",
    "downlink_failures",
    "snr_db",
    "noise_sigma",
    "downlink_mcs",
    "uplink_mcs",
    "downlink_bler_percent",
    "uplink_bler_percent",
    "downlink_prbs",
    "uplink_prbs",
    "downlink_retransmissions",
    "uplink_retransmissions",
    "channel_quality_indicator",
    "uplink_snr_db",
    "downlink_mac_mbps",
    "uplink_mac_mbps",
    "downlink_buffer_bytes",
    "uplink_buffer_bytes",
    "connection_failures",
    "amf_restart_count_before",
    "amf_restart_count_after",
    "amf_memory_max_bytes",
]


T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def mean_or_none(values):
    values = [float(value) for value in values if value is not None]
    return statistics.mean(values) if values else None


def throughput_mean(summary, direction):
    return mean_or_none(
        item[direction]["megabits_per_second"]
        for item in summary["throughput"]
    )


def throughput_failures(summary, direction):
    return sum(
        item[direction].get("status") == "failed"
        for item in summary["throughput"]
    )


def ran_mean(summary, name):
    return summary["ran"]["metrics"].get(name, {}).get("mean")


def ran_total(summary, name):
    return summary["ran"]["metrics"].get(name, {}).get("total")


def divide(value, divisor):
    return None if value is None else float(value) / divisor


def combined_ping(summary):
    pings = [
        item["ping"]
        for item in summary["pings"]
    ]
    if not pings:
        raise ValueError("trial summary has no per-UE ping results")
    transmitted = sum(int(item["transmitted"]) for item in pings)
    received = sum(int(item["received"]) for item in pings)
    weighted_rtt = [
        (
            item["rtt_ms"]["mean"],
            int(item["received"]),
        )
        for item in pings
        if item.get("rtt_ms") is not None
        and item["rtt_ms"]["mean"] is not None
    ]
    rtt_mean = (
        None
        if not weighted_rtt or received == 0
        else sum(value * count for value, count in weighted_rtt)
        / sum(count for value, count in weighted_rtt)
    )
    p95_values = [
        item["rtt_ms"]["p95"]
        for item in pings
        if item.get("rtt_ms") is not None
        and item["rtt_ms"]["p95"] is not None
    ]
    return {
        "transmitted": transmitted,
        "received": received,
        "packet_loss_percent": (
            None
            if transmitted == 0
            else 100.0 * (transmitted - received) / transmitted
        ),
        "rtt_ms": {
            "mean": rtt_mean,
            "p95": max(p95_values) if p95_values else None,
        },
    }


def flatten_trial(summary):
    ping = combined_ping(summary)
    rtt = ping["rtt_ms"]
    return {
        "condition_id": summary["condition_id"],
        "trial_number": summary["trial_number"],
        "status": summary["status"],
        "link_status": summary.get("link_status", "unknown"),
        "attachment_success": summary["attachment_success"],
        "ue_ips": ";".join(
            item["ue_ip"] for item in summary["ue_ips"]
        ),
        "ping_transmitted": ping["transmitted"],
        "ping_received": ping["received"],
        "packet_loss_percent": ping["packet_loss_percent"],
        "rtt_mean_ms": rtt["mean"],
        "rtt_p95_ms": rtt["p95"],
        "uplink_mbps": throughput_mean(summary, "uplink"),
        "downlink_mbps": throughput_mean(summary, "downlink"),
        "uplink_failures": throughput_failures(summary, "uplink"),
        "downlink_failures": throughput_failures(
            summary, "downlink"
        ),
        "snr_db": summary["noise"]["snr_db"],
        "noise_sigma": summary["noise"]["noise_sigma"],
        "downlink_mcs": ran_mean(summary, "downlink_mcs"),
        "uplink_mcs": ran_mean(summary, "uplink_mcs"),
        "downlink_bler_percent": ran_mean(
            summary, "downlink_bler_percent"
        ),
        "uplink_bler_percent": ran_mean(
            summary, "uplink_bler_percent"
        ),
        "downlink_prbs": ran_mean(summary, "downlink_prbs"),
        "uplink_prbs": ran_mean(summary, "uplink_prbs"),
        "downlink_retransmissions": ran_total(
            summary, "downlink_harq_retransmission_grants"
        ),
        "uplink_retransmissions": ran_total(
            summary, "uplink_harq_retransmission_grants"
        ),
        "channel_quality_indicator": ran_mean(
            summary, "channel_quality_indicator"
        ),
        "uplink_snr_db": ran_mean(summary, "uplink_snr_db"),
        "downlink_mac_mbps": divide(
            ran_mean(summary, "downlink_mac_bitrate_bps"),
            1_000_000.0,
        ),
        "uplink_mac_mbps": divide(
            ran_mean(summary, "uplink_mac_bitrate_bps"),
            1_000_000.0,
        ),
        "downlink_buffer_bytes": ran_mean(
            summary, "downlink_buffer_bytes"
        ),
        "uplink_buffer_bytes": ran_mean(
            summary, "uplink_buffer_bytes"
        ),
        "connection_failures": summary["connection_failures"],
        "amf_restart_count_before": summary["amf"]["restart_count_before"],
        "amf_restart_count_after": summary["amf"]["restart_count_after"],
        "amf_memory_max_bytes": summary["amf"]["memory_max_observed"],
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
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
            "sample_stddev": None,
            "mean_95_percent_ci": None,
        }
    deviation = statistics.stdev(values) if len(values) > 1 else None
    interval = None
    if deviation is not None:
        degrees = len(values) - 1
        critical = T_CRITICAL_95[degrees]
        margin = critical * deviation / math.sqrt(len(values))
        center = statistics.mean(values)
        interval = {
            "low": center - margin,
            "high": center + margin,
            "margin": margin,
            "method": "two-sided Student t",
        }
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
        "sample_stddev": deviation,
        "mean_95_percent_ci": interval,
    }


def wilson_interval(failures, total):
    if total <= 0:
        return None
    z = 1.959963984540054
    proportion = failures / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(
        proportion * (1.0 - proportion) / total
        + z * z / (4.0 * total * total)
    ) / denominator
    return {
        "low_percent": 100.0 * max(0.0, center - margin),
        "high_percent": 100.0 * min(1.0, center + margin),
        "method": "Wilson score",
        "packet_count": total,
    }


def ue_rows(summaries):
    rows = []
    for summary in summaries:
        throughput = {
            item["ue_index"]: item
            for item in summary["throughput"]
        }
        pings = summary["pings"]
        for item in pings:
            ue_index = item["ue_index"]
            ping = item["ping"]
            rtt = ping.get("rtt_ms") or {}
            rates = throughput.get(ue_index, {})
            rows.append({
                "condition_id": summary["condition_id"],
                "trial_number": summary["trial_number"],
                "ue_index": ue_index,
                "packet_loss_percent": ping["packet_loss_percent"],
                "rtt_mean_ms": rtt.get("mean"),
                "uplink_mbps": rates.get("uplink", {}).get(
                    "megabits_per_second"
                ),
                "downlink_mbps": rates.get("downlink", {}).get(
                    "megabits_per_second"
                ),
                "uplink_status": rates.get("uplink", {}).get("status"),
                "downlink_status": rates.get("downlink", {}).get(
                    "status"
                ),
            })
    return rows


def numeric(value):
    match = re.search(r"-?[0-9.]+", str(value))
    return None if match is None else float(match.group())


def required_numeric(value, field):
    result = numeric(value)
    if result is None:
        raise ValueError(f"GPU sample has no numeric {field}")
    return result


def process_rows(trial_path, condition_id, trial_number):
    path = trial_path / "condition/monitoring/processes.jsonl"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        sample = json.loads(line)
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
                "gpu_index": int(required_numeric(
                    normalized.get("index"), "index"
                )),
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
    per_ue_rows = ue_rows(summaries)
    write_csv(
        summary_dir / "ue-results.csv",
        [
            "condition_id", "trial_number", "ue_index",
            "packet_loss_percent", "rtt_mean_ms",
            "uplink_mbps", "downlink_mbps",
            "uplink_status", "downlink_status",
        ],
        per_ue_rows,
    )
    grouped = {}
    for row in rows:
        grouped.setdefault(row["condition_id"], []).append(row)
    conditions = []
    for condition_id, condition_rows in grouped.items():
        transmitted = sum(
            int(row.get("ping_transmitted") or 0)
            for row in condition_rows
        )
        received = sum(
            int(row.get("ping_received") or 0)
            for row in condition_rows
        )
        conditions.append({
            "condition_id": condition_id,
            "trial_count": len(condition_rows),
            "successful_trials": sum(row["status"] == "passed" for row in condition_rows),
            "link_outage_trials": sum(
                row["link_status"] == "outage"
                for row in condition_rows
            ),
            "degraded_link_trials": sum(
                row["link_status"] == "degraded"
                for row in condition_rows
            ),
            "packet_loss_percent": aggregate(condition_rows, "packet_loss_percent"),
            "packet_loss_95_percent_ci": wilson_interval(
                transmitted - received, transmitted
            ),
            "rtt_mean_ms": aggregate(condition_rows, "rtt_mean_ms"),
            "uplink_mbps": aggregate(condition_rows, "uplink_mbps"),
            "downlink_mbps": aggregate(
                condition_rows, "downlink_mbps"
            ),
            "uplink_failures": sum(
                row["uplink_failures"] for row in condition_rows
            ),
            "downlink_failures": sum(
                row["downlink_failures"] for row in condition_rows
            ),
            "downlink_mcs": aggregate(condition_rows, "downlink_mcs"),
            "uplink_mcs": aggregate(condition_rows, "uplink_mcs"),
            "downlink_bler_percent": aggregate(
                condition_rows, "downlink_bler_percent"
            ),
            "uplink_bler_percent": aggregate(
                condition_rows, "uplink_bler_percent"
            ),
            "downlink_prbs": aggregate(
                condition_rows, "downlink_prbs"
            ),
            "uplink_prbs": aggregate(condition_rows, "uplink_prbs"),
            "downlink_retransmissions": aggregate(
                condition_rows, "downlink_retransmissions"
            ),
            "uplink_retransmissions": aggregate(
                condition_rows, "uplink_retransmissions"
            ),
            "channel_quality_indicator": aggregate(
                condition_rows, "channel_quality_indicator"
            ),
            "uplink_snr_db": aggregate(
                condition_rows, "uplink_snr_db"
            ),
            "downlink_mac_mbps": aggregate(
                condition_rows, "downlink_mac_mbps"
            ),
            "uplink_mac_mbps": aggregate(
                condition_rows, "uplink_mac_mbps"
            ),
            "downlink_buffer_bytes": aggregate(
                condition_rows, "downlink_buffer_bytes"
            ),
            "uplink_buffer_bytes": aggregate(
                condition_rows, "uplink_buffer_bytes"
            ),
            "individual_trials_always_reported": True,
            "confidence_level_percent": 95,
            "confidence_interval_note": (
                "Mean intervals require at least two trials; "
                "packet loss uses all ping packets."
            ),
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
            sample = json.loads(line)
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
    dot_plot(summary_dir / "plots/uplink-throughput.svg", "Uplink throughput by individual trial", rows, "uplink_mbps", "Throughput (Mbit/s)")
    dot_plot(summary_dir / "plots/downlink-throughput.svg", "Downlink throughput by individual trial", rows, "downlink_mbps", "Throughput (Mbit/s)")
    dot_plot(summary_dir / "plots/cpu.svg", "CPU by individual process sample", resources, "cpu_percent", "CPU (%)")
    dot_plot(summary_dir / "plots/gpu-utilization.svg", "GPU utilization samples", gpu_samples, "gpu_utilization_percent", "GPU utilization (%)")
    line_plot(summary_dir / "plots/amf-memory.svg", "AMF memory during pilot", amf_samples, "time_ns", "memory_current", "Time (ns)", "Memory (bytes)")
    atomic_write_text(
        summary_dir / "README.txt",
        "Individual trial results are shown in trials.csv.\n"
        "Mean 95% intervals use Student's t distribution.\n"
        "Packet-loss intervals use the Wilson score method.\n",
    )
    return {
        "trial_rows": rows,
        "conditions": conditions,
        "ue_rows": per_ue_rows,
        "moving_positions": moving_positions,
        "failures": failures,
        "resources": resources,
        "gpu_samples": gpu_samples,
        "amf_samples": amf_samples,
    }
