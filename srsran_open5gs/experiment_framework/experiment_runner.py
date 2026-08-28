#!/usr/bin/env python3

import hashlib
import json
import os
import pathlib
import re
import signal
import socket
import time

from .config import DEFERRED_THROUGHPUT, REPO_ROOT, apply_propagation
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
        self.parameters = resolved_study.get("parameters", {})
        self.channel = self.parameters.get("channel", {})
        self.timeouts = self.parameters.get("timeouts", {})
        self.namespace = namespace
        self.host_python = self.parameters.get("host_python", "python3")
        self.store = None
        self.amf = None
        self.resource_monitor = None
        self.backgrounds = []
        self.executor = CommandExecutor(cwd=REPO_ROOT, safety_check=self.check_safety)
        self.lifecycle = None
        self.deployment_changed = False
        self.current_condition = None
        self.current_trial = None
        self._normal_shutdown = False

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
        runtime = self.parameters.get("runtime_images", {})
        if runtime.get("preflight_enabled"):
            image_key = runtime.get("image_key")
            image = (
                self.study.get("runtime_images", {}).get(image_key)
                or runtime.get("images", {}).get(image_key)
            )
            if not image:
                raise CommandFailure("runtime image preflight is enabled but no image is configured")
            archive = image["archive"]
            actual = self.executor.capture(["sudo", "sha256sum", archive]).split()[0]
            if actual != image["archive_sha256"]:
                raise CommandFailure("runtime image archive checksum mismatch")
            image_list = self.executor.capture(["sudo", "ctr", "-n", "k8s.io", "images", "list"])
            if image["reference"] not in image_list:
                self.executor.run(
                    ["sudo", "ctr", "-n", "k8s.io", "images", "import", archive],
                    provenance / "runtime-image-import.log",
                    timeout=600,
                )
                image_list = self.executor.capture(["sudo", "ctr", "-n", "k8s.io", "images", "list"])
            matching = [line for line in image_list.splitlines() if line.split() and line.split()[0] == image["reference"]]
            if not matching or image["digest"] not in matching[0]:
                raise CommandFailure("runtime image digest is not available in k8s.io")
            atomic_write_text(provenance / "containerd-runtime-image.txt", matching[0] + "\n")
        else:
            atomic_write_text(provenance / "containerd-runtime-image.txt", "runtime image preflight disabled by benchmark parameters\n")

        for condition in self.study["conditions"]:
            overlay = condition.get("overlay")
            if not overlay:
                continue
            rendered = self.executor.capture(["kubectl", "kustomize", str(REPO_ROOT / overlay)])
            output = provenance / "rendered-overlays" / f"{condition['condition_id']}.yaml"
            atomic_write_text(output, rendered + "\n")
            if re.search(r"(?m)^\s*type:\s*NodePort\s*$", rendered):
                raise CommandFailure(f"NodePort found in {condition['condition_id']} overlay")

    def amf_slice(self, start_index):
        samples = self.amf.samples()
        selected = samples[start_index:]
        if not selected:
            selected = samples[-1:]
        if not selected:
            return {}
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
        deadline = time.monotonic() + float(self.channel.get("port_forward_ready_seconds", 10))
        while time.monotonic() < deadline:
            background.check()
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return background
            except OSError:
                time.sleep(0.2)
        raise CommandFailure("port-forward did not become ready")

    def stop_backgrounds(self):
        for background in reversed(self.backgrounds):
            try:
                background.stop()
            except Exception:
                pass
        self.backgrounds.clear()

    def start_continuous_ping(self):
        self.lifecycle.start_background_ping(
            self.lifecycle.logs["continuous_ping"],
            interval=float(self.channel.get("continuous_ping_interval_seconds", 0.05)),
            deadline=None,
            count=None,
        )

    def stop_continuous_ping(self, trial_dir):
        self.lifecycle.stop_background_ping(
            interval=float(self.channel.get("continuous_ping_interval_seconds", 0.05)),
        )
        self.checked_sleep(1)
        output = self.lifecycle.ue_capture(f"cat {self.lifecycle.shell_quote(self.lifecycle.logs['continuous_ping'])} 2>/dev/null || true", check=False)
        path = pathlib.Path(trial_dir) / "condition/traffic/continuous-ping.txt"
        atomic_write_text(path, output + "\n")
        return parse_ping(output) if output.strip() else None

    def run_host(self, command, output_log, timeout=300):
        self.executor.run(command, output_log, timeout=timeout)

    def _placement_args(self, condition, trial_number):
        scene = self.parameters.get("scene", {})
        if not scene.get("randomize_positions", False):
            return []
        if condition.get("placement_seed") is not None:
            seed = int(condition["placement_seed"])
        else:
            base = int(scene.get("placement_seed", 0))
            material = f"{self.study['study_id']}:{condition['condition_id']}:{trial_number}".encode()
            offset = int(hashlib.sha256(material).hexdigest()[:8], 16)
            seed = base + offset
        arguments = ["--placement-mode", "random", "--placement-seed", str(seed)]
        if scene.get("min_link_distance_m") is not None:
            arguments += ["--placement-min-distance", str(float(scene["min_link_distance_m"]))]
        return arguments

    def throughput_record(self):
        return dict(DEFERRED_THROUGHPUT)

    def _resolve_scene(self, condition, trial_dir):
        channel_dir = pathlib.Path(trial_dir) / "condition/channel"
        source = pathlib.Path(condition["scene_resolved"]["absolute_path"])
        scene = json.loads(source.read_text(encoding="utf-8"))
        if condition.get("scene_overrides"):
            scene = _deep_merge(scene, condition["scene_overrides"])
        merged = apply_propagation(scene, condition.get("propagation"))
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
        self.run_host(
            [
                self.host_python,
                str(REPO_ROOT / "channel_emulation/moving_sionna_controller.py"),
                "--trajectory", condition["trajectory_resolved"]["absolute_path"],
                "--scene-config", scene_path,
                "--num-ues", str(self.lifecycle.num_ues),
                "--endpoint", CONTROL_ENDPOINT,
                "--stream-endpoint", STREAM_ENDPOINT,
                "--final-hold-seconds", str(self.channel.get("final_hold_seconds", 5.0)),
                "--output", str(live),
                *self._placement_args(condition, trial_number),
            ],
            channel_dir / "moving-channel.log",
            timeout=float(self.channel.get("moving_live_timeout_seconds", 300)),
        )
        continuous = self.stop_continuous_ping(trial_dir)
        final = condition["measurement_profile_resolved"]["values"]["final_ping"]
        pings = self.final_ping_per_ue(trial_dir, final)
        return {
            "live": json.loads(live.read_text(encoding="utf-8")),
            "continuous_ping": continuous,
            "ping": pings[0]["ping"],
            "pings": pings,
        }

    def run_channel(self, condition, trial_dir, trial_number):
        scene_path = self._resolve_scene(condition, trial_dir)
        return self.run_moving(condition, trial_dir, trial_number, scene_path)

    def connection_failure_count(self, trial_dir):
        patterns = re.compile(r"error|failed|underflow|overflow|underrun|overrun|timeout|dropped", re.I)
        total = 0
        for path in (pathlib.Path(trial_dir) / "condition/logs").glob("*.log"):
            total += sum(bool(patterns.search(line)) for line in path.read_text(encoding="utf-8", errors="replace").splitlines())
        return total

    def trial_summary(self, condition, trial_number, ue_ips, result, amf_start):
        ping = result.get("ping") or result.get("continuous_ping")
        if isinstance(ue_ips, list) and len(ue_ips) == 1:
            ue_ip_field = ue_ips[0].get("ue_ip")
        else:
            ue_ip_field = ue_ips
        return {
            "condition_id": condition["condition_id"],
            "trial_number": trial_number,
            "status": "passed",
            "attachment_success": True,
            "ue_ip": ue_ip_field,
            "ping": ping,
            "pings": result.get("pings"),
            "connection_failures": 0,
            "amf": self.amf_slice(amf_start),
            "throughput": self.throughput_record(),
        }

    def run_condition(self, condition, trial_number):
        trial_dir = self.store.trial(condition["condition_id"], trial_number)
        self.current_condition = condition["condition_id"]
        self.current_trial = trial_number
        write_json(trial_dir / "resolved-condition.json", condition)
        amf_start = len(self.amf.samples())
        failure = None
        try:
            self.deployment_changed = True
            self.lifecycle.apply_overlay(condition["overlay"], trial_dir / "condition/deployment")
            ue_ip = self.lifecycle.start_radio(condition, trial_dir)
            interval = condition["measurement_profile_resolved"]["values"].get(
                "resource_interval_seconds",
                self.parameters.get("monitoring", {}).get("process_interval_seconds", 1.0),
            )
            self.resource_monitor = ResourceMonitor(self.lifecycle, trial_dir, interval)
            self.resource_monitor.start()
            result = self.run_channel(condition, trial_dir, trial_number)
            self.resource_monitor.check()
            write_json(trial_dir / "condition/result.json", result)
            self.lifecycle.capture_logs(trial_dir)
            summary = self.trial_summary(condition, trial_number, ue_ip, result, amf_start)
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
            if self.resource_monitor is not None:
                try:
                    self.resource_monitor.stop()
                finally:
                    self.resource_monitor = None
            self.stop_backgrounds()
            try:
                self.lifecycle.capture_logs(trial_dir)
            except Exception:
                pass
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
            if not self.study["conditions"]:
                summarize_run(self.store.root)
                self.store.write_checksums()
                return self.store.root

            self.configure_kubernetes()
            self.lifecycle.save_original(self.store.root / "provenance/original-cluster-state")
            interval = min(
                condition["measurement_profile_resolved"]["values"].get("amf_interval_seconds", 0.5)
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
                for condition in self.study["conditions"]:
                    for trial_number in range(1, self.study["trials_per_condition"] + 1):
                        self.run_condition(condition, trial_number)
                self._normal_shutdown = True
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
