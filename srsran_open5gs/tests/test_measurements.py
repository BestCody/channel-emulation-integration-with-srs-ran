import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiment_framework.ran_metrics import summarize_ran_metrics
from experiment_framework.summarize import aggregate
from experiment_framework.summarize import flatten_trial
from experiment_framework.summarize import wilson_interval
from experiment_framework.throughput import parse_iperf3_json


class MeasurementTests(unittest.TestCase):
    def test_iperf3_result_is_converted_to_mbps(self):
        payload = {
            "end": {
                "sum_sent": {
                    "bits_per_second": 12_000_000,
                    "bytes": 1_500_000,
                    "seconds": 1.0,
                    "retransmits": 2,
                },
                "sum_received": {
                    "bits_per_second": 11_500_000,
                    "bytes": 1_437_500,
                    "seconds": 1.0,
                },
            }
        }
        result = parse_iperf3_json(json.dumps(payload), "uplink")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["megabits_per_second"], 11.5)
        self.assertEqual(result["retransmits"], 2)

    def test_iperf3_connection_failure_is_measurement_data(self):
        payload = {"error": "unable to connect to server"}
        result = parse_iperf3_json(json.dumps(payload), "uplink")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["megabits_per_second"], 0.0)
        self.assertEqual(result["bytes"], 0)

    def test_ran_metrics_extract_known_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "metrics.jsonl"
            records = [
                {
                    "captured_at_ns": 1,
                    "payload": {"ue_list": [{"ue_container": {
                        "dl_mcs": 10,
                        "ul_nof_ok": 98,
                        "ul_nof_nok": 2,
                    }}]},
                },
                {
                    "captured_at_ns": 2,
                    "payload": {"ue_list": [{"ue_container": {
                        "dl_mcs": 14,
                        "ul_nof_ok": 96,
                        "ul_nof_nok": 4,
                    }}]},
                },
            ]
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            scheduler = pathlib.Path(directory) / "scheduler.log"
            scheduler.write_text(
                "[SCHED] [I] DL: ue=0 rnti=0x4601 "
                "rb=[4..14) rv=0 tbs=100\n"
                "[SCHED] [I] UL: ue=0 rnti=0x4601 "
                "rb=[8..13) rv=1 tbs=50\n"
                "2026-08-29T00:\n",
                encoding="utf-8",
            )
            summary = summarize_ran_metrics(path, scheduler)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["metrics"]["downlink_mcs"]["mean"], 12)
        self.assertEqual(
            summary["metrics"]["uplink_bler_percent"]["mean"], 3
        )
        self.assertEqual(summary["metrics"]["downlink_prbs"]["mean"], 10)
        self.assertEqual(
            summary["metrics"]
            ["uplink_harq_retransmission_grants"]["total"],
            1,
        )

    def test_student_t_interval_is_reported(self):
        result = aggregate([{"value": 1}, {"value": 3}], "value")
        self.assertEqual(result["mean"], 2)
        self.assertIsNotNone(result["mean_95_percent_ci"])

    def test_derived_bler_is_weighted_by_decoded_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "metrics.jsonl"
            records = [
                {
                    "captured_at_ns": 1,
                    "payload": {
                        "ue_list": [
                            {"ue_container": {
                                "dl_nof_ok": 9,
                                "dl_nof_nok": 1,
                            }}
                        ]
                    }
                },
                {
                    "captured_at_ns": 2,
                    "payload": {
                        "ue_list": [
                            {"ue_container": {
                                "dl_nof_ok": 50,
                                "dl_nof_nok": 50,
                            }}
                        ]
                    }
                },
            ]
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            summary = summarize_ran_metrics(path)
        bler = summary["metrics"]["downlink_bler_percent"]
        self.assertAlmostEqual(bler["mean"], 100 * 51 / 110)
        self.assertEqual(bler["failed_blocks"], 51)
        self.assertEqual(bler["block_count"], 110)

    def test_incomplete_iperf_result_is_rejected(self):
        payload = {"end": {"sum_sent": {"bits_per_second": 1}}}
        with self.assertRaises(ValueError):
            parse_iperf3_json(json.dumps(payload), "uplink")

    def test_wilson_interval_is_bounded(self):
        result = wilson_interval(5, 100)
        self.assertLess(result["low_percent"], 5)
        self.assertGreater(result["high_percent"], 5)

    def test_noise_axis_is_in_trial_table(self):
        row = flatten_trial({
            "condition_id": "snr-20db",
            "trial_number": 1,
            "status": "passed",
            "attachment_success": True,
            "noise": {"snr_db": 20.0, "noise_sigma": None},
            "ue_ips": [{"ue_index": 1, "ue_ip": "10.45.0.2/24"}],
            "pings": [{
                "ue_index": 1,
                "ping": {
                    "transmitted": 1,
                    "received": 1,
                    "packet_loss_percent": 0.0,
                    "rtt_ms": {"mean": 1.0, "p95": 1.0},
                },
            }],
            "throughput": [],
            "ran": {"metrics": {}},
            "connection_failures": 0,
            "amf": {
                "restart_count_before": 0,
                "restart_count_after": 0,
                "memory_max_observed": 1,
            },
        })
        self.assertEqual(row["snr_db"], 20.0)
        self.assertIsNone(row["noise_sigma"])

    def test_complete_ping_outage_has_no_rtt(self):
        row = flatten_trial({
            "condition_id": "snr-15db",
            "trial_number": 1,
            "status": "passed",
            "link_status": "outage",
            "attachment_success": True,
            "noise": {"snr_db": 15.0, "noise_sigma": None},
            "ue_ips": [{"ue_index": 1, "ue_ip": "10.45.0.2/24"}],
            "pings": [{
                "ue_index": 1,
                "ping": {
                    "transmitted": 5,
                    "received": 0,
                    "packet_loss_percent": 100.0,
                    "rtt_ms": None,
                },
            }],
            "throughput": [],
            "ran": {"metrics": {}},
            "connection_failures": 5,
            "amf": {
                "restart_count_before": 0,
                "restart_count_after": 0,
                "memory_max_observed": 1,
            },
        })
        self.assertEqual(row["packet_loss_percent"], 100.0)
        self.assertIsNone(row["rtt_mean_ms"])


if __name__ == "__main__":
    unittest.main()
