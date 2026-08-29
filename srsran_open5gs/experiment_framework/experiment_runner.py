#!/usr/bin/env python3

import hashlib
import json
import os
import pathlib
import re
import signal
import socket
import time

from .config import REPO_ROOT, apply_propagation
from .settings import (
    _deep_merge,
    CONTROL_ENDPOINT,
    PORT_FORWARD,
    PORT_FORWARD_HOST,
    PORT_FORWARD_PORT,
    PORT_FORWARD_STREAM,
    STREAM_ENDPOINT,
)
from .failures import FailureRecord
from .lifecycle import (
    AMFMonitor,
    BackgroundCommand,
    CommandExecutor,
    CommandFailure,
    KubernetesLifecycle,
    ResourceMonitor,
    SafetyStop,
)
from .provenance import collect_provenance
from .results import ResultStore, atomic_write_text, write_json
from .summarize import summarize_run
from .ping_parsing import parse_ping
from .ran_metrics import summarize_ran_metrics


def _port_forward_mappings():
    mappings = []
    for value in (PORT_FORWARD, PORT_FORWARD_STREAM):
        if value and value not in mappings:
            mappings.append(value)
    return tuple(mappings)


class StudyLock:
    def __init__(self, result_root):
        self.path = pathlib.Path(result_root) / ".evaluation.lock"
        self.descriptor = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(self.descriptor, f"pid={os.getpid()}\n".encode())
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.descriptor is not None:
            os.close(self.descriptor)
        self.path.unlink(missing_ok=True)


class PilotRunner:
    def __init__(self, resolved_study, *, namespace=None):
        self.study = resolved_study
        self.parameters = resolved_study["parameters"]
        self.channel = self.parameters["channel"]
        self.timeouts = self.parameters["timeouts"]
        self.namespace = namespace
        self.host_python = self.parameters["host_python"]
        self.store = None
        self.amf = None
        self.resource_monitor = None
        self.backgrounds = []
        self.executor = CommandExecutor(cwd=REPO_ROOT, safety_check=self.check_safety)
        self.lifecycle = None
        self.deployment_changed = False

    def configure_kubernetes(self):
        if self.lifecycle is not None:
            return
        if not self.namespace:
            raise ValueError("Kubernetes namespace is required for condition runs")
        self.lifecycle = KubernetesLifecycle(
            REPO_ROOT,
            self.namespace,
            self.executor,
            self.parameters,
        )

    def check_safety(self):
        if self.amf is not None:
            self.amf.check()
        if self.resource_monitor is not None:
            self.resource_monitor.check()
        for background in self.backgrounds:
            background.check()

    def checked_sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_safety()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def install_signal_handlers(self):
        def interrupt(signum, frame):
            raise InterruptedError(f"received signal {signum}")
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)

    def preflight(self):
        provenance = self.store.root / "provenance"
        for condition in self.study["conditions"]:
            overlay = condition["overlay"]
            rendered = self.executor.capture(["kubectl", "kustomize", str(REPO_ROOT / overlay)])
            output = provenance / "rendered-overlays" / f"{condition['condition_id']}.yaml"
            atomic_write_text(output, rendered + "\n")
            if re.search(r"(?m)^\s*type:\s*NodePort\s*$", rendered):
                raise CommandFailure(f"NodePort found in {condition['condition_id']} overlay")

    def amf_slice(self, start_index):
        samples = self.amf.samples()
        selected = samples[start_index:]
        if not selected:
            raise SafetyStop("AMF monitor produced no trial samples")
        return {
            "restart_count_before": selected[0]["restart_count"],
            "restart_count_after": selected[-1]["restart_count"],
            "memory_first_bytes": selected[0]["memory_current"],
            "memory_last_bytes": selected[-1]["memory_current"],
            "memory_max_observed": max(item["memory_current"] for item in selected),
            "memory_limit_bytes": selected[-1]["memory_max"],
            "pod_uids": sorted(set(item["pod_uid"] for item in selected)),
            "sample_count": len(selected),
        }

    def start_port_forward(self, trial_dir):
        self.lifecycle.ue_pod = self.lifecycle.discover_ue()
        port_forward = _port_forward_mappings()
        host = PORT_FORWARD_HOST
        port = PORT_FORWARD_PORT
        background = BackgroundCommand(
            [
                "kubectl",
                "port-forward",
                "-n",
                self.namespace,
                f"pod/{self.lifecycle.ue_pod}",
                *port_forward,
            ],
            REPO_ROOT,
            pathlib.Path(trial_dir) / "condition/logs/port-forward.log",
        )
        self.backgrounds.append(background)
        deadline = time.monotonic() + float(
            self.channel["port_forward_ready_seconds"]
        )
        while time.monotonic() < deadline:
            background.check()
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return background
            except OSError:
                time.sleep(0.2)
        raise CommandFailure("port-forward did not become ready")

    def stop_backgrounds(self):
        errors = []
        for background in reversed(self.backgrounds):
            try:
                background.stop()
            except Exception as error:
                errors.append(error)
        self.backgrounds.clear()
        if errors:
            raise CommandFailure(
                f"failed to stop {len(errors)} background process(es): "
                f"{errors[0]}"
            ) from errors[0]

    def start_continuous_ping(self):
        for ue_index in range(1, self.lifecycle.num_ues + 1):
            path = self.lifecycle.logs["continuous_ping"]
            if self.lifecycle.num_ues > 1:
                root, extension = os.path.splitext(path)
                path = f"{root}-ue{ue_index}{extension}"
            self.lifecycle.start_background_ping(
                path,
                interval=float(
                    self.channel["continuous_ping_interval_seconds"]
                ),
                deadline=None,
                count=None,
                ue_index=ue_index,
            )

    def stop_continuous_ping(self, trial_dir):
        self.lifecycle.stop_background_ping(
            interval=float(
                self.channel["continuous_ping_interval_seconds"]
            ),
        )
        self.checked_sleep(1)
        results = []
        for ue_index in range(1, self.lifecycle.num_ues + 1):
            source = self.lifecycle.logs["continuous_ping"]
            name = "continuous-ping.txt"
            if self.lifecycle.num_ues > 1:
                root, extension = os.path.splitext(source)
                source = f"{root}-ue{ue_index}{extension}"
                name = f"continuous-ping-ue{ue_index}.txt"
            output = self.lifecycle.ue_capture(
                f"cat {self.lifecycle.shell_quote(source)}",
            )
            if not output.strip():
                raise CommandFailure(
                    f"UE {ue_index} continuous ping produced no output"
                )
            ping = parse_ping(output)
            if ping["reply_count"] < 1:
                raise CommandFailure(
                    f"UE {ue_index} continuous ping received no replies"
                )
            path = pathlib.Path(trial_dir) / "condition/traffic" / name
            atomic_write_text(path, output + "\n")
            results.append({
                "ue_index": ue_index,
                "ping": ping,
            })
        return results

    def throughput_per_ue(self, condition, trial_dir):
        profile = condition["measurement_profile_resolved"]["values"]
        settings = profile["throughput"]
        if not settings["enabled"]:
            return []
        traffic = pathlib.Path(trial_dir) / "condition/traffic"
        results = []
        for ue_index in range(1, self.lifecycle.num_ues + 1):
            measurements = {}
            for direction in ("uplink", "downlink"):
                path = traffic / f"iperf3-{direction}-ue{ue_index}.json"
                measurements[direction] = self.lifecycle.iperf(
                    path,
                    direction=direction,
                    duration=settings["duration_seconds"],
                    omit=settings["omit_seconds"],
                    connect_timeout=settings[
                        "connect_timeout_seconds"
                    ],
                    completion_timeout=settings[
                        "completion_timeout_seconds"
                    ],
                    ue_index=ue_index,
                )
            results.append({
                "ue_index": ue_index,
                **measurements,
            })
        return results

    def _placement_args(self, condition, trial_number):
        scene = self.parameters["scene"]
        mode = condition["placement_mode"]
        if mode == "configured":
            return ["--placement-mode", "configured"]
        if condition.get("placement_seed") is not None:
            seed = int(condition["placement_seed"])
        else:
            base = int(scene["placement_seed"])
            material = f"{self.study['study_id']}:{condition['condition_id']}:{trial_number}".encode()
            offset = int(hashlib.sha256(material).hexdigest()[:8], 16)
            seed = base + offset
        arguments = [
            "--placement-mode", "random",
            "--placement-seed", str(seed),
        ]
        arguments += [
            "--placement-min-distance",
            str(float(scene["min_link_distance_m"])),
        ]
        return arguments

    @staticmethod
    def _noise_args(condition):
        noise = condition.get("noise", {})
        if "sigma" in noise:
            return ["--noise-sigma", str(float(noise["sigma"]))]
        if "snr_db" in noise:
            return ["--snr-db", str(float(noise["snr_db"]))]
        return []

    def _resolve_scene(self, condition, trial_dir):
        channel_dir = pathlib.Path(trial_dir) / "condition/channel"
        source = pathlib.Path(condition["scene_resolved"]["absolute_path"])
        scene = json.loads(source.read_text(encoding="utf-8"))
        if condition.get("scene_overrides"):
            scene = _deep_merge(scene, condition["scene_overrides"])
        merged = apply_propagation(scene, condition["propagation"])
        resolved = channel_dir / "resolved-scene.json"
        write_json(resolved, merged)
        return str(resolved)

    def final_ping_per_ue(self, trial_dir, final):
        traffic = pathlib.Path(trial_dir) / "condition/traffic"
        pings = []
        for ue_index in range(1, self.lifecycle.num_ues + 1):
            name = (
                "final-ping.txt" if self.lifecycle.num_ues == 1
                else f"final-ping-ue{ue_index}.txt"
            )
            ping = self.lifecycle.ping(
                traffic / name,
                count=final["count"],
                interval=final["interval_seconds"],
                deadline=final["deadline_seconds"],
                ue_index=ue_index,
            )
            pings.append({"ue_index": ue_index, "ping": ping})
        return pings

    def run_moving(self, condition, trial_dir, trial_number, scene_path):
        self.start_port_forward(trial_dir)
        channel_dir = pathlib.Path(trial_dir) / "condition/channel"
        self.start_continuous_ping()
        live = channel_dir / "moving-channel.json"
        self.executor.run(
            [
                self.host_python,
                str(REPO_ROOT / "channel_emulation/moving_sionna_controller.py"),
                "--trajectory", condition["trajectory_resolved"]["absolute_path"],
                "--scene-config", scene_path,
                "--gnb-config",
                str(
                    REPO_ROOT
                    / "configs/srsRAN/srsran-gnb/config/srsran-gnb.yaml"
                ),
                "--radio-config",
                str(REPO_ROOT / "configs/ues/srsue/config/radio.json"),
                "--num-ues", str(self.lifecycle.num_ues),
                "--endpoint", CONTROL_ENDPOINT,
                "--stream-endpoint", STREAM_ENDPOINT,
                "--control-timeout-ms",
                str(self.channel["control_timeout_ms"]),
                "--interp-steps",
                str(self.channel["interpolation_steps"]),
                "--final-hold-seconds",
                str(self.channel["final_hold_seconds"]),
                "--output", str(live),
                *self._placement_args(condition, trial_number),
                *self._noise_args(condition),
            ],
            channel_dir / "moving-channel.log",
            timeout=float(self.channel["moving_live_timeout_seconds"]),
        )
        continuous = self.stop_continuous_ping(trial_dir)
        throughput = self.throughput_per_ue(condition, trial_dir)
        final = condition["measurement_profile_resolved"]["values"]["final_ping"]
        pings = self.final_ping_per_ue(trial_dir, final)
        return {
            "live": json.loads(live.read_text(encoding="utf-8")),
            "continuous_pings": continuous,
            "pings": pings,
            "throughput": throughput,
        }

    def run_channel(self, condition, trial_dir, trial_number):
        scene_path = self._resolve_scene(condition, trial_dir)
        return self.run_moving(condition, trial_dir, trial_number, scene_path)

    def connection_failure_count(self, trial_dir):
        patterns = re.compile(r"error|failed|underflow|overflow|underrun|overrun|timeout|dropped", re.I)
        ignored = "Failed to register file descriptor. fd=0"
        total = 0
        for path in (pathlib.Path(trial_dir) / "condition/logs").glob("*.log"):
            total += sum(
                bool(patterns.search(line)) and ignored not in line
                for line in path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            )
        return total

    def trial_summary(
        self,
        condition,
        trial_number,
        ue_ips,
        result,
        amf_start,
        trial_dir,
        measurement_start_ns,
        measurement_end_ns,
    ):
        ran = summarize_ran_metrics(
            pathlib.Path(trial_dir)
            / "condition/monitoring/gnb-metrics.jsonl",
            pathlib.Path(trial_dir)
            / "condition/logs/gnb-scheduler.log",
            start_ns=measurement_start_ns,
            end_ns=measurement_end_ns,
        )
        if ran["sample_count"] < 1:
            raise CommandFailure("gNB JSON metrics were not captured")
        if ran["scheduler"]["grant_count"] < 1:
            raise CommandFailure("gNB scheduler grants were not captured")
        noise = result["live"]["noise"]
        throughput_statuses = [
            item[direction]["status"]
            for item in result["throughput"]
            for direction in ("uplink", "downlink")
        ]
        failed_throughput = sum(
            status == "failed" for status in throughput_statuses
        )
        ping_transmitted = sum(
            item["ping"]["transmitted"] for item in result["pings"]
        )
        ping_received = sum(
            item["ping"]["received"] for item in result["pings"]
        )
        if throughput_statuses and failed_throughput == len(
            throughput_statuses
        ):
            link_status = "outage"
        elif failed_throughput or ping_received < ping_transmitted:
            link_status = "degraded"
        else:
            link_status = "available"
        return {
            "condition_id": condition["condition_id"],
            "trial_number": trial_number,
            "status": "passed",
            "link_status": link_status,
            "attachment_success": True,
            "ue_ips": ue_ips,
            "pings": result["pings"],
            "throughput": result["throughput"],
            "noise": noise,
            "ran": ran,
            "measurement_window_ns": {
                "start": measurement_start_ns,
                "end": measurement_end_ns,
            },
            "connection_failures": 0,
            "amf": self.amf_slice(amf_start),
        }

    def run_condition(self, condition, trial_number):
        trial_dir = self.store.trial(condition["condition_id"], trial_number)
        write_json(trial_dir / "resolved-condition.json", condition)
        amf_start = len(self.amf.samples())
        failure = None
        try:
            self.deployment_changed = True
            self.lifecycle.apply_overlay(condition["overlay"], trial_dir / "condition/deployment")
            ue_ip = self.lifecycle.start_radio(condition, trial_dir)
            interval = condition["measurement_profile_resolved"]["values"][
                "resource_interval_seconds"
            ]
            self.resource_monitor = ResourceMonitor(self.lifecycle, trial_dir, interval)
            self.resource_monitor.start()
            measurement_start_ns = time.time_ns()
            result = self.run_channel(condition, trial_dir, trial_number)
            measurement_end_ns = time.time_ns()
            result["measurement_window_ns"] = {
                "start": measurement_start_ns,
                "end": measurement_end_ns,
            }
            self.resource_monitor.check()
            write_json(trial_dir / "condition/result.json", result)
            self.lifecycle.capture_logs(trial_dir)
            summary = self.trial_summary(
                condition,
                trial_number,
                ue_ip,
                result,
                amf_start,
                trial_dir,
                measurement_start_ns,
                measurement_end_ns,
            )
            summary["connection_failures"] = self.connection_failure_count(trial_dir)
            write_json(trial_dir / "summary.json", summary)
        except BaseException as error:
            failure = error
            record = FailureRecord(
                category="amf_safety" if isinstance(error, SafetyStop) else "unexpected",
                message=str(error),
                condition_id=condition["condition_id"],
                trial_number=trial_number,
                command=getattr(error, "command", None),
                return_code=getattr(error, "return_code", None),
            )
            write_json(trial_dir / "failure.json", record.to_dict())
        finally:
            cleanup_errors = []
            if self.resource_monitor is not None:
                try:
                    self.resource_monitor.stop()
                except BaseException as error:
                    cleanup_errors.append(error)
                finally:
                    self.resource_monitor = None
            try:
                self.stop_backgrounds()
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                self.lifecycle.capture_logs(trial_dir)
            except BaseException as error:
                cleanup_errors.append(error)
            if cleanup_errors:
                write_json(
                    trial_dir / "cleanup-failures.json",
                    [
                        {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        for error in cleanup_errors
                    ],
                )
                if failure is None:
                    failure = cleanup_errors[0]
                    (trial_dir / "summary.json").unlink(
                        missing_ok=True
                    )
                    write_json(
                        trial_dir / "failure.json",
                        FailureRecord(
                            category="unexpected",
                            message=str(failure),
                            condition_id=condition["condition_id"],
                            trial_number=trial_number,
                            command=getattr(failure, "command", None),
                            return_code=getattr(
                                failure, "return_code", None
                            ),
                        ).to_dict(),
                    )
            try:
                with self.executor.without_safety_checks():
                    self.lifecycle.restore(trial_dir / "restoration")
                self.deployment_changed = False
            except BaseException as restore_error:
                write_json(
                    trial_dir / "restoration/failure.json",
                    FailureRecord(
                        category="restoration",
                        message=str(restore_error),
                        condition_id=condition["condition_id"],
                        trial_number=trial_number,
                    ).to_dict(),
                )
                raise
        if failure is not None:
            recovery = self.store.root / "failure-recovery" / f"{condition['condition_id']}-trial-{trial_number:03d}"
            if isinstance(failure, SafetyStop):
                write_json(recovery / "summary.json", {
                    "status": "not-run-amf-safety-stop",
                    "reason": "The study stopped and restored immediately; starting another radio test at an AMF safety threshold would violate the stop condition",
                })
            else:
                recovery_result = self.lifecycle.baseline_check(recovery, ping_count=20)
                write_json(recovery / "summary.json", recovery_result)
            raise failure

    def run(self):
        self.install_signal_handlers()
        with StudyLock(self.study["result_root"]):
            self.store = ResultStore(self.study["result_root"], self.study["study_id"])
            self.store.write_json("resolved-study.json", self.study)
            collect_provenance(self.store.root / "provenance", REPO_ROOT, self.study, self.parameters)
            self.preflight()
            self.configure_kubernetes()
            self.lifecycle.save_original(self.store.root / "provenance/original-cluster-state")
            self.lifecycle.validate_integration(
                self.store.root / "provenance"
            )
            interval = min(
                condition["measurement_profile_resolved"]["values"][
                    "amf_interval_seconds"
                ]
                for condition in self.study["conditions"]
            )
            self.amf = AMFMonitor(
                REPO_ROOT,
                self.namespace,
                self.store.root / "monitoring",
                interval,
                self.host_python,
                self.parameters,
            )
            self.amf.start()
            try:
                baseline = self.lifecycle.baseline_check(
                    self.store.root / "pre-pilot-baseline",
                    ping_count=20,
                )
                write_json(
                    self.store.root / "pre-pilot-baseline/summary.json",
                    baseline,
                )
                if baseline["status"] != "passed":
                    raise CommandFailure("pre-pilot baseline failed")
                for condition in self.study["conditions"]:
                    for trial_number in range(1, self.study["trials_per_condition"] + 1):
                        self.run_condition(condition, trial_number)
                baseline = self.lifecycle.baseline_check(
                    self.store.root / "post-pilot-baseline",
                    ping_count=20,
                )
                write_json(
                    self.store.root / "post-pilot-baseline/summary.json",
                    baseline,
                )
                if baseline["status"] != "passed":
                    raise CommandFailure("post-pilot baseline failed")
            finally:
                self.stop_backgrounds()
                if self.resource_monitor is not None:
                    self.resource_monitor.stop()
                    self.resource_monitor = None
                if self.deployment_changed:
                    with self.executor.without_safety_checks():
                        self.lifecycle.restore(self.store.root / "emergency-restoration")
                    self.deployment_changed = False
                if self.amf is not None:
                    self.amf.stop()

            summarize_run(self.store.root)
            self.store.write_checksums()
            return self.store.root
