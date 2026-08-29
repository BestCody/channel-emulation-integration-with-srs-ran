import json
import math
import pathlib
import re
import statistics
from datetime import datetime, timezone


FIELDS = {
    "dl_mcs": "downlink_mcs",
    "ul_mcs": "uplink_mcs",
    "cqi": "channel_quality_indicator",
    "pusch_snr_db": "uplink_snr_db",
    "dl_brate": "downlink_mac_bitrate_bps",
    "ul_brate": "uplink_mac_bitrate_bps",
    "dl_bs": "downlink_buffer_bytes",
    "bsr": "uplink_buffer_bytes",
    "dl_nof_ok": "downlink_harq_acks",
    "dl_nof_nok": "downlink_harq_nacks",
    "ul_nof_ok": "uplink_crc_successes",
    "ul_nof_nok": "uplink_crc_failures",
}

SCHEDULER_GRANT = re.compile(
    r"\b(?P<direction>DL|UL):.*?\brnti=0x(?P<rnti>[0-9a-fA-F]+)"
    r".*?\brb=\[(?P<start>\d+)\.\.(?P<end>\d+)\)"
    r".*?\brv=(?P<rv>\d+)"
)
SCHEDULER_TIME = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)\s"
)


def scalar_leaves(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from scalar_leaves(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from scalar_leaves(item, (*path, str(index)))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            yield path, number


def canonical_name(path):
    key = path[-1].lower() if path else ""
    return FIELDS.get(key)


def summarize_values(values):
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def scheduler_time_ns(line):
    match = SCHEDULER_TIME.match(line)
    if not match:
        return None
    value = datetime.fromisoformat(match.group(1))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1_000_000_000)


def scheduler_metrics(path, start_ns=None, end_ns=None):
    path = pathlib.Path(path)
    allocations = {"downlink": [], "uplink": []}
    retransmissions = {"downlink": 0, "uplink": 0}
    per_rnti = {}
    if not path.exists():
        return {
            "grant_count": 0,
            "metrics": {},
            "per_rnti": {},
        }
    for line in path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        timestamp = scheduler_time_ns(line)
        if start_ns is not None and timestamp is not None:
            if timestamp < start_ns:
                continue
        if end_ns is not None and timestamp is not None:
            if timestamp > end_ns:
                continue
        for match in SCHEDULER_GRANT.finditer(line):
            direction = (
                "downlink"
                if match.group("direction") == "DL"
                else "uplink"
            )
            prbs = int(match.group("end")) - int(match.group("start"))
            rnti = match.group("rnti").lower()
            allocations[direction].append(prbs)
            bucket = per_rnti.setdefault(
                rnti, {"downlink": [], "uplink": []}
            )
            bucket[direction].append(prbs)
            if int(match.group("rv")) > 0:
                retransmissions[direction] += 1
    metrics = {}
    for direction, values in allocations.items():
        if values:
            metrics[f"{direction}_prbs"] = summarize_values(values)
            metrics[
                f"{direction}_harq_retransmission_grants"
            ] = {
                "count": len(values),
                "total": retransmissions[direction],
                "percent": 100.0
                * retransmissions[direction]
                / len(values),
            }
    return {
        "grant_count": sum(len(values) for values in allocations.values()),
        "metrics": metrics,
        "per_rnti": {
            rnti: {
                direction: summarize_values(values)
                for direction, values in directions.items()
                if values
            }
            for rnti, directions in per_rnti.items()
        },
    }


def derived_link_metrics(records):
    counts = {
        "downlink_bler_percent": {
            "reports": 0,
            "successes": 0,
            "failures": 0,
            "percentages": [],
        },
        "uplink_bler_percent": {
            "reports": 0,
            "successes": 0,
            "failures": 0,
            "percentages": [],
        },
    }
    for record in records:
        payload = record["payload"]
        for item in payload["ue_list"]:
            ue = item["ue_container"]
            for direction, prefix in (
                ("downlink", "dl"), ("uplink", "ul")
            ):
                ok = ue.get(f"{prefix}_nof_ok")
                nok = ue.get(f"{prefix}_nof_nok")
                if ok is None or nok is None or ok + nok == 0:
                    continue
                metric = counts[f"{direction}_bler_percent"]
                metric["reports"] += 1
                metric["successes"] += int(ok)
                metric["failures"] += int(nok)
                metric["percentages"].append(100.0 * nok / (ok + nok))
    results = {}
    for name, metric in counts.items():
        if not metric["reports"]:
            continue
        total = metric["successes"] + metric["failures"]
        results[name] = {
            "count": metric["reports"],
            "mean": 100.0 * metric["failures"] / total,
            "minimum": min(metric["percentages"]),
            "maximum": max(metric["percentages"]),
            "successful_blocks": metric["successes"],
            "failed_blocks": metric["failures"],
            "block_count": total,
            "aggregation": "failed blocks divided by decoded blocks",
        }
    return results


def summarize_ran_metrics(
    path, scheduler_path=None, start_ns=None, end_ns=None
):
    path = pathlib.Path(path)
    records = []
    canonical = {}
    raw = {}
    if not path.exists():
        return {"sample_count": 0, "metrics": {}, "raw_fields": {}}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            record = json.loads(line)
            captured = int(record["captured_at_ns"])
            payload = record["payload"]
            if not isinstance(payload["ue_list"], list):
                raise TypeError("ue_list is not a list")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"invalid gNB metric record on line {line_number}"
            ) from error
        if start_ns is not None:
            if int(captured) < int(start_ns):
                continue
        if end_ns is not None:
            if int(captured) > int(end_ns):
                continue
        records.append(record)
        for field_path, number in scalar_leaves(payload):
            path_name = ".".join(field_path)
            raw.setdefault(path_name, []).append(number)
            name = canonical_name(field_path)
            if name:
                canonical.setdefault(name, []).append(number)
    metrics = {
        key: summarize_values(values)
        for key, values in sorted(canonical.items())
    }
    metrics.update(derived_link_metrics(records))
    scheduler = scheduler_metrics(
        scheduler_path, start_ns=start_ns, end_ns=end_ns
    ) if scheduler_path else {
        "grant_count": 0,
        "metrics": {},
        "per_rnti": {},
    }
    metrics.update(scheduler["metrics"])
    return {
        "sample_count": len(records),
        "metrics": metrics,
        "scheduler": scheduler,
        "raw_fields": {
            key: summarize_values(values)
            for key, values in sorted(raw.items())
        },
    }
