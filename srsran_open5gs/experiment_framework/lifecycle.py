#!/usr/bin/env python3

import json
import os
import pathlib
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from .results import atomic_write_text, write_json
from .ping_parsing import parse_ping
from .throughput import parse_iperf3_json
from .settings import (
    FLOWGRAPH_PROCESS_PATTERN,
    GATEWAY,
    GNB_PROCESS_PATTERN,
    START_GNB_SCRIPT,
    START_GNU_SCRIPT,
    START_UE_SCRIPT,
    TUN_INTERFACE,
    UE_PROCESS_PATTERN,
)


class CommandFailure(RuntimeError):
    def __init__(self, message, command=None, return_code=None):
        super().__init__(message)
        self.command = command
        self.return_code = return_code


def command_environment(env=None):
    return dict(os.environ if env is None else env)


def kubernetes_image_names(nodes):
    return {
        name
        for node in nodes.get("items", [])
        for image in node.get("status", {}).get("images", []) or []
        for name in image.get("names") or []
    }


class SafetyStop(RuntimeError):
    pass


class CommandExecutor:
    def __init__(self, *, cwd, safety_check=None):
        self.cwd = str(cwd)
        self.safety_check = safety_check

    def check_safety(self):
        if self.safety_check is not None:
            self.safety_check()

    @contextmanager
    def without_safety_checks(self):
        previous = self.safety_check
        self.safety_check = None
        try:
            yield
        finally:
            self.safety_check = previous

    def run(self, command, log_path, *, timeout=300, check=True, env=None):
        log_path = pathlib.Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                env=command_environment(env),
                text=True,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    self.check_safety()
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise CommandFailure("command timed out", list(command), None)
                    time.sleep(0.2)
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        self.check_safety()
        if check and process.returncode != 0:
            raise CommandFailure(
                f"command failed with exit code {process.returncode}",
                list(command),
                process.returncode,
            )
        return process.returncode

    def capture(self, command, *, timeout=30, check=True):
        self.check_safety()
        started = time.monotonic()
        with (
            tempfile.TemporaryFile(
                mode="w+", encoding="utf-8"
            ) as output,
            tempfile.TemporaryFile(
                mode="w+", encoding="utf-8"
            ) as errors,
        ):
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                env=command_environment(),
                text=True,
                stdout=output,
                stderr=errors,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    self.check_safety()
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise CommandFailure("command timed out", list(command), None)
                    time.sleep(0.1)
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
            output.seek(0)
            captured = output.read()
            errors.seek(0)
            captured_errors = errors.read()
        self.check_safety()
        if check and process.returncode != 0:
            raise CommandFailure(
                captured_errors.strip()
                or captured.strip()
                or "command failed",
                list(command),
                process.returncode,
            )
        return captured.strip()


@dataclass(frozen=True)
class OriginalUEState:
    replicas: int
    configmap: str
    image: str
    pull_policy: str


class AMFMonitor:
    def __init__(self, repo_root, namespace, output_dir, interval, host_python, parameters):
        self.repo_root = pathlib.Path(repo_root)
        self.namespace = namespace
        self.output_dir = pathlib.Path(output_dir)
        self.interval = float(interval)
        self.host_python = host_python
        self.parameters = parameters
        self.selector = parameters["kubernetes"]["amf_selector"]
        self.process = None
        self.stopping = False

    @property
    def samples_path(self):
        return self.output_dir / "amf-memory.jsonl"

    @property
    def summary_path(self):
        return self.output_dir / "amf-monitor-summary.json"

    def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.host_python,
            str(self.repo_root / "channel_emulation/amf_memory_monitor.py"),
            "--namespace", self.namespace,
            "--interval", str(self.interval),
            "--output", str(self.samples_path),
            "--summary", str(self.summary_path),
        ]
        command.extend(["--selector", self.selector])
        safety = self.parameters["amf_safety"]
        command.extend([
            "--stop-growth-bytes",
            str(safety["stop_at_growth_bytes"]),
            "--warn-growth-bytes",
            str(safety["warn_at_growth_bytes"]),
            "--stop-limit-fraction",
            str(safety["stop_at_limit_fraction"]),
            "--warn-limit-fraction",
            str(safety["warn_at_limit_fraction"]),
        ])
        log = (self.output_dir / "amf-monitor.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._log_handle = log
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.samples_path.exists() and self.samples_path.stat().st_size:
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.2)
        self.check()
        raise SafetyStop("AMF monitor did not produce its baseline sample")

    def check(self):
        if self.process is None:
            return
        return_code = self.process.poll()
        if return_code is None:
            return
        if self.stopping and return_code == 0:
            return
        reason = "AMF monitor stopped unexpectedly"
        if self.summary_path.exists():
            try:
                summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
                if summary["reason"]:
                    reason = summary["reason"]
            except (OSError, KeyError, json.JSONDecodeError) as error:
                raise SafetyStop(
                    f"AMF monitor summary is invalid: {error}"
                ) from error
        raise SafetyStop(reason)

    def samples(self):
        values = []
        if not self.samples_path.exists():
            return values
        for number, line in enumerate(
            self.samples_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise SafetyStop(
                    f"invalid AMF sample on line {number}: {error}"
                ) from error
            if "memory_current" in value:
                values.append(value)
        return values

    def stop(self):
        if self.process is None:
            return
        self.stopping = True
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        self._log_handle.close()
        if self.process.returncode != 0:
            self.stopping = False
            self.check()


class BackgroundCommand:
    def __init__(self, command, cwd, log_path):
        self.log = pathlib.Path(log_path).open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def check(self):
        if self.process.poll() is not None:
            raise CommandFailure("background command stopped", return_code=self.process.returncode)

    def stop(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.log.close()


class ResourceMonitor:
    def __init__(self, lifecycle, trial_dir, interval):
        self.lifecycle = lifecycle
        self.trial_dir = pathlib.Path(trial_dir)
        self.interval = float(interval)
        self.stop_event = threading.Event()
        self.failure = None
        self.thread = None
        self.gpu = None

    def start(self):
        monitoring = self.trial_dir / "condition/monitoring"
        monitoring.mkdir(parents=True, exist_ok=True)
        monitor_config = self.lifecycle.parameters["monitoring"]
        self.gpu = BackgroundCommand(
            [
                monitor_config["nvidia_smi"],
                "--query-gpu=timestamp,index,uuid,utilization.gpu,utilization.memory,memory.used,power.draw,temperature.gpu",
                "--format=csv",
                "-lms", str(monitor_config["gpu_query_interval_ms"]),
            ],
            self.lifecycle.repo_root,
            monitoring / "gpu.csv",
        )
        self.thread = threading.Thread(target=self._run, name="evaluation-resource-monitor", daemon=True)
        self.thread.start()

    def _run(self):
        output = self.trial_dir / "condition/monitoring/processes.jsonl"
        identity = self.lifecycle.radio_identity()
        while not self.stop_event.wait(self.interval):
            try:
                current = self.lifecycle.radio_identity()
                if current != identity:
                    raise SafetyStop(f"radio process identity changed: {identity} -> {current}")
                sample = {
                    "time_ns": time.time_ns(),
                    "identity": current,
                    "ue_ps": self.lifecycle.ue_capture(
                        f"ps -p '{current['flowgraph_pid']}','{current['ue_pid']}' -o pid,ppid,pcpu,pmem,rss,vsz,etime,args"
                    ),
                    "gnb_ps": self.lifecycle.gnb_capture(
                        f"ps -p '{current['gnb_pid']}' -o pid,ppid,pcpu,pmem,rss,vsz,etime,args"
                    ),
                }
                with output.open("a", encoding="utf-8") as destination:
                    destination.write(json.dumps(sample, sort_keys=True) + "\n")
            except BaseException as error:
                self.failure = error
                return

    def check(self):
        if self.failure:
            raise self.failure
        if self.gpu:
            self.gpu.check()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.gpu:
            self.gpu.stop()


class KubernetesLifecycle:
    def __init__(self, repo_root, namespace, executor, parameters):
        self.repo_root = pathlib.Path(repo_root)
        self.namespace = namespace
        self.executor = executor
        self.parameters = parameters
        self.channel = parameters["channel"]
        self.kubernetes = parameters["kubernetes"]
        self.radio = parameters["radio"]
        self.logs = parameters["logs"]
        self.timeouts = parameters["timeouts"]
        self.original = None
        self.ue_pod = None
        self.gnb_pod = None
        self.ue_selector = self.kubernetes["ue_selector"]
        self.gnb_selector = self.kubernetes["gnb_selector"]
        self.ue_deployment = self.kubernetes["ue_deployment"]
        self.ue_container = self.kubernetes["ue_container"]
        self.gnb_container = self.kubernetes["gnb_container"]
        self.upf_selector = self.kubernetes["upf_selector"]
        self.iperf_container = self.kubernetes["iperf_container"]
        self.iperf_port = int(self.kubernetes["iperf_port"])
        self.ue_config_volume = self.kubernetes["ue_config_volume"]
        self.baseline_overlay = self.kubernetes["baseline_overlay"]
        self.baseline_script = self.kubernetes["baseline_script"]
        self.num_ues = int(self.radio["ue_number"])
        self.secondary_interface = self.radio["secondary_interface"]
        self.gateway = GATEWAY
        self.tun_interface = TUN_INTERFACE
        self.flowgraph_pattern = FLOWGRAPH_PROCESS_PATTERN
        self.ue_process_pattern = UE_PROCESS_PATTERN
        self.gnb_process_pattern = GNB_PROCESS_PATTERN

    @staticmethod
    def shell_quote(value):
        return shlex.quote(str(value))

    def ue_netns_for(self, ue_index):
        return f"ue{int(ue_index)}"

    def ue_log_path(self, ue_index):
        base = self.logs["ue"]
        if self.num_ues == 1:
            return base
        root, ext = os.path.splitext(base)
        return f"{root}-ue{int(ue_index)}{ext}"

    def kubectl(self, *arguments):
        return ["kubectl", *arguments]

    def capture(self, *arguments, check=True, timeout=30):
        return self.executor.capture(
            self.kubectl(*arguments),
            check=check,
            timeout=timeout,
        )

    def discover_ue(self):
        return self.capture(
            "get", "pods", "-n", self.namespace,
            "-l", self.ue_selector,
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        )

    def discover_gnb(self):
        return self.capture(
            "get", "pods", "-n", self.namespace,
            "-l", self.gnb_selector,
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        )

    def save_original(self, output_dir):
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.executor.run(
            self.kubectl("get", "deployment", self.ue_deployment, "-n", self.namespace, "-o", "yaml"),
            output_dir / "original-deployment.yaml",
        )
        self.original = self.current_state()
        write_json(output_dir / "original-state.json", asdict(self.original))
        return self.original

    def validate_integration(self, output_dir):
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        deployments = json.loads(self.capture(
            "get", "deployments", "-n", self.namespace,
            "-l", "app=open5gs", "-o", "json",
        ))
        failures = []
        records = []
        for deployment in deployments.get("items", []):
            metadata = deployment.get("metadata", {})
            spec = deployment.get("spec", {})
            status = deployment.get("status", {})
            desired = int(spec.get("replicas", 0))
            available = int(status.get("availableReplicas", 0))
            record = {
                "name": metadata.get("name"),
                "desired_replicas": desired,
                "available_replicas": available,
            }
            records.append(record)
            if available < desired:
                failures.append(
                    f"{record['name']} has {available}/{desired} "
                    "available replicas"
                )
        if not records:
            failures.append("no Open5GS deployments were found")

        upf_pods = json.loads(self.capture(
            "get", "pods", "-n", self.namespace,
            "-l", self.upf_selector, "-o", "json",
        ))
        iperf_ready = False
        for pod in upf_pods.get("items", []):
            statuses = pod.get("status", {}).get("containerStatuses", [])
            iperf_ready = iperf_ready or any(
                status.get("name") == self.iperf_container
                and status.get("ready") is True
                for status in statuses
            )
        if not iperf_ready:
            failures.append("UPF iperf3 server container is not ready")

        required = self.parameters["runtime_images"][
            "required_in_kubernetes"
        ]
        nodes = json.loads(self.capture("get", "nodes", "-o", "json"))
        images = kubernetes_image_names(nodes)
        for image in required:
            if image not in images:
                failures.append(
                    f"required Kubernetes image is missing: {image}"
                )

        self.gnb_pod = self.discover_gnb()
        interface = self.shell_quote(self.secondary_interface)
        address = self.gnb_capture(
            f"ip -4 -o address show dev {interface}", check=False
        )
        if not address:
            failures.append(
                f"gNB pod has no {self.secondary_interface} interface"
            )

        report = {
            "open5gs_deployments": records,
            "required_kubernetes_images": list(required),
            "gnb_pod": self.gnb_pod,
            "gnb_secondary_interface": address,
            "upf_iperf_ready": iperf_ready,
            "failures": failures,
        }
        write_json(output_dir / "integration-preflight.json", report)
        if failures:
            raise CommandFailure(
                "integration preflight failed: " + "; ".join(failures)
            )
        return report

    def ue_capture(self, script, check=True, timeout=30):
        self.ue_pod = self.ue_pod or self.discover_ue()
        return self.capture(
            "exec", "-n", self.namespace, self.ue_pod,
            "-c", self.ue_container, "--", "bash", "-lc", script,
            check=check,
            timeout=timeout,
        )

    def gnb_capture(self, script, check=True):
        self.gnb_pod = self.gnb_pod or self.discover_gnb()
        return self.capture("exec", "-n", self.namespace, self.gnb_pod, "-c", self.gnb_container, "--", "bash", "-lc", script, check=check)

    def stop_radio(self):
        self.ue_pod = self.capture(
            "get", "pods", "-n", self.namespace, "-l", self.ue_selector,
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
            check=False,
        )
        self.gnb_pod = self.capture(
            "get", "pods", "-n", self.namespace, "-l", self.gnb_selector,
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
            check=False,
        )
        stop_sleep = float(self.timeouts["radio_stop_sleep_seconds"])
        if self.ue_pod:
            self.capture(
                "exec", "-n", self.namespace, self.ue_pod, "-c", self.ue_container, "--",
                "bash", "-lc",
                f"pkill -TERM -f '[p]ing .*{self.shell_quote(self.gateway)}' 2>/dev/null || true; "
                f"pkill -INT -f {self.shell_quote(self.ue_process_pattern)} 2>/dev/null || true",
                check=False,
            )
        time.sleep(stop_sleep)
        if self.gnb_pod:
            self.capture(
                "exec", "-n", self.namespace, self.gnb_pod, "-c", self.gnb_container, "--",
                "bash", "-lc",
                f"pkill -INT -f {self.shell_quote(self.gnb_process_pattern)} 2>/dev/null || true; "
                "pkill -TERM -f '[c]apture_gnb_metrics.py' 2>/dev/null || true",
                check=False,
            )
        time.sleep(stop_sleep)
        if self.ue_pod:
            self.capture(
                "exec", "-n", self.namespace, self.ue_pod, "-c", self.ue_container, "--",
                "bash", "-lc",
                f"pkill -INT -f {self.shell_quote(self.flowgraph_pattern)} 2>/dev/null || true",
                check=False,
            )

    def wait_no_ue(self):
        timeout = float(self.timeouts["ue_wait_gone_seconds"])
        force_after = float(self.timeouts["ue_force_delete_after_seconds"])
        started = time.monotonic()
        deadline = started + timeout
        forced = False
        while time.monotonic() < deadline:
            value = self.capture("get", "pods", "-n", self.namespace, "-l", self.ue_selector, "-o", "name", check=False)
            if not value.strip():
                return
            if not forced and time.monotonic() - started >= force_after:
                self.capture(
                    "delete", "pods", "-n", self.namespace, "-l", self.ue_selector,
                    "--grace-period=0", "--force", "--wait=false",
                    check=False,
                )
                forced = True
            time.sleep(1)
        raise CommandFailure("UE pod did not disappear")

    def apply_overlay(self, overlay, output_dir):
        if self.original is None:
            raise RuntimeError("original deployment state was not saved")
        output_dir = pathlib.Path(output_dir)
        self.stop_radio()
        self.executor.run(
            self.kubectl("scale", f"deployment/{self.ue_deployment}", "-n", self.namespace, "--replicas=0"),
            output_dir / "scale-zero.log",
        )
        self.wait_no_ue()
        self.executor.run(
            self.kubectl("apply", "-k", str(self.repo_root / overlay), "-n", self.namespace),
            output_dir / "apply.log",
        )
        self.executor.run(
            self.kubectl("scale", f"deployment/{self.ue_deployment}", "-n", self.namespace, f"--replicas={self.original.replicas}"),
            output_dir / "scale.log",
        )
        rollout = int(self.timeouts["rollout_seconds"])
        self.executor.run(
            self.kubectl("rollout", "status", f"deployment/{self.ue_deployment}", "-n", self.namespace, f"--timeout={rollout}s"),
            output_dir / "rollout.log",
            timeout=rollout + 10,
        )
        self.executor.run(
            self.kubectl("wait", "--for=condition=Ready", "pod", "-l", self.ue_selector, "-n", self.namespace, f"--timeout={rollout}s"),
            output_dir / "ready.log",
            timeout=rollout + 10,
        )
        self.ue_pod = self.discover_ue()
        self.gnb_pod = self.discover_gnb()
        interface = self.shell_quote(self.secondary_interface)
        if not self.ue_capture(
            f"ip -4 -o address show dev {interface}", check=False
        ):
            raise CommandFailure(
                f"replacement UE pod has no "
                f"{self.secondary_interface} interface"
            )
        wrapper = self.ue_capture(
            f"pgrep -af "
            f"{self.shell_quote(self.flowgraph_pattern + '|' + self.ue_process_pattern)}",
            check=False,
        )
        atomic_write_text(output_dir / "wrapper-process-check.txt", wrapper + ("\n" if wrapper else ""))
        if wrapper:
            raise CommandFailure("replacement UE pod started radio processes automatically")

    def start_radio(self, condition, trial_dir):
        trial_dir = pathlib.Path(trial_dir)
        launcher = condition["launcher"]
        gnuradio_log = self.shell_quote(self.logs["gnuradio"])
        gnb_log = self.shell_quote(self.logs["gnb"])
        self.ue_capture(f"nohup {launcher} >{gnuradio_log} 2>&1 </dev/null &")
        time.sleep(float(self.timeouts["radio_start_gnuradio_sleep_seconds"]))
        flow = self.ue_capture(f"pgrep -f {self.shell_quote(self.flowgraph_pattern)} | head -n1")
        if not flow:
            raise CommandFailure("GNU Radio process did not remain running")
        metrics = self.shell_quote(self.logs["gnb_metrics"])
        capture_log = self.shell_quote(self.logs["gnb_metrics_capture"])
        self.gnb_capture(
            "pkill -TERM -f '[c]apture_gnb_metrics.py' "
            "2>/dev/null || true",
            check=False,
        )
        time.sleep(0.2)
        self.gnb_capture(
            "nohup python3 /srsran/config/capture_gnb_metrics.py "
            f"--bind {self.shell_quote(self.radio['metrics_bind'])} "
            f"--port {int(self.radio['metrics_port'])} "
            f"--output {metrics} >{capture_log} 2>&1 </dev/null &"
        )
        timeout = condition["measurement_profile_resolved"]["values"][
            "attachment_timeout_seconds"
        ]
        for ue_index in range(1, self.num_ues + 1):
            self._launch_one_ue(ue_index)
        for ue_index in range(1, self.num_ues + 1):
            self._wait_one_ue_ready(ue_index, timeout)
        self.gnb_capture(
            f"nohup {START_GNB_SCRIPT} >{gnb_log} 2>&1 </dev/null &"
        )
        self._wait_gnb_ready(timeout)
        phrase = self.shell_quote(self.radio["attachment_log_phrase"])
        ue_ips = []
        for ue_index in range(1, self.num_ues + 1):
            ue_ips.append(
                self._wait_one_ue(
                    ue_index, trial_dir, timeout, phrase
                )
            )
        return ue_ips

    def _launch_one_ue(self, ue_index):
        ue_log = self.shell_quote(self.ue_log_path(ue_index))
        self.ue_capture(
            f"nohup {START_UE_SCRIPT} {ue_index} "
            f">{ue_log} 2>&1 </dev/null &"
        )

    def _wait_one_ue_ready(self, ue_index, timeout):
        ue_log = self.shell_quote(self.ue_log_path(ue_index))
        phrase = self.shell_quote(self.radio["ue_ready_log_phrase"])
        process = self.shell_quote(
            "/tmp/ue_" + str(ue_index) + ".conf"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ue_capture(
                f"grep -Fq {phrase} {ue_log}; printf '%s' $?",
                check=False,
            ) == "0":
                return
            running = self.ue_capture(
                f"pgrep -af {self.shell_quote(self.ue_process_pattern)} "
                f"| grep -F {process}",
                check=False,
            )
            if not running:
                raise CommandFailure(
                    f"UE {ue_index} process exited before radio readiness"
                )
            time.sleep(1)
        raise CommandFailure(f"UE {ue_index} radio readiness timed out")

    def _wait_gnb_ready(self, timeout):
        log = self.shell_quote(self.logs["gnb_scheduler"])
        phrase = self.shell_quote(self.radio["gnb_ready_log_phrase"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.gnb_capture(
                f"grep -Fq {phrase} {log}; printf '%s' $?",
                check=False,
            ) == "0":
                return
            running = self.gnb_capture(
                f"pgrep -af {self.shell_quote(self.gnb_process_pattern)}",
                check=False,
            )
            if not running:
                raise CommandFailure(
                    "gNB process exited before radio readiness"
                )
            time.sleep(1)
        raise CommandFailure("gNB radio readiness timed out")

    def _wait_one_ue(self, ue_index, trial_dir, timeout, phrase):
        ue_log = self.shell_quote(self.ue_log_path(ue_index))
        netns = self.ue_netns_for(ue_index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ue_capture(f"grep -Fq {phrase} {ue_log}; printf '%s' $?", check=False) == "0":
                break
            process = self.ue_capture(
                f"pgrep -af {self.shell_quote(self.ue_process_pattern)} "
                f"| grep -F "
                f"{self.shell_quote('/tmp/ue_' + str(ue_index) + '.conf')}",
                check=False,
            )
            if not process:
                raise CommandFailure(f"UE {ue_index} process exited before attachment")
            time.sleep(1)
        else:
            raise CommandFailure(f"UE {ue_index} attachment timed out")
        self.ue_capture(f"ip netns exec {self.shell_quote(netns)} ip route replace default via {self.shell_quote(self.gateway)}")
        ue_ip = self.ue_capture(f"ip netns exec {self.shell_quote(netns)} ip -4 -o addr show dev {self.shell_quote(self.tun_interface)} | awk '{{print $4}}'")
        atomic_write_text(trial_dir / f"condition/ue-ip-{ue_index}.txt", ue_ip + "\n")
        return {"ue_index": ue_index, "ue_ip": ue_ip}

    def radio_identity(self):
        self.ue_pod = self.discover_ue()
        self.gnb_pod = self.discover_gnb()
        return {
            "ue_pod": self.ue_pod,
            "pod_uid": self.capture("get", "pod", self.ue_pod, "-n", self.namespace, "-o", "jsonpath={.metadata.uid}"),
            "flowgraph_pid": self.ue_capture(f"pgrep -f {self.shell_quote(self.flowgraph_pattern)} | head -n1"),
            "ue_pid": self.ue_capture(f"pgrep -f {self.shell_quote(self.ue_process_pattern)} | head -n1"),
            "gnb_pod": self.gnb_pod,
            "gnb_pid": self.gnb_capture(f"pgrep -f {self.shell_quote(self.gnb_process_pattern)} | head -n1"),
        }

    def ping(self, output_path, *, count, interval, deadline, ue_index):
        netns = self.ue_netns_for(ue_index)
        output = self.ue_capture(
            f"ip netns exec {self.shell_quote(netns)} ping -D -i {float(interval):g} -c {int(count)} -W {deadline} {self.shell_quote(self.gateway)}",
            check=False,
        )
        atomic_write_text(output_path, output + "\n")
        return parse_ping(output)

    def iperf(
        self,
        output_path,
        *,
        direction,
        duration,
        omit,
        connect_timeout,
        completion_timeout,
        ue_index,
    ):
        if direction not in {"uplink", "downlink"}:
            raise ValueError("iperf direction must be uplink or downlink")
        netns = self.ue_netns_for(ue_index)
        reverse = " --reverse" if direction == "downlink" else ""
        result_path = self.shell_quote(
            f"/tmp/iperf3-{direction}-ue{ue_index}.json"
        )
        limit = float(completion_timeout)
        command = (
            f"ip netns exec {self.shell_quote(netns)} iperf3 "
            f"--client {self.shell_quote(self.gateway)} "
            f"--port {self.iperf_port} --json "
            f"--connect-timeout {int(1000 * float(connect_timeout))} "
            f"--time {float(duration):g} --omit {float(omit):g}{reverse}"
        )
        output = self.ue_capture(
            f"if timeout --signal=TERM --kill-after=2s {limit:g}s "
            f"{command} >{result_path}; then cat {result_path}; "
            "else rc=$?; "
            "if [ \"$rc\" -eq 124 ] || [ \"$rc\" -eq 137 ]; then "
            "printf '%s' '{\"error\":\"measurement timed out\"}'; "
            f"else cat {result_path}; fi; fi",
            check=False,
            timeout=limit + 5.0,
        )
        atomic_write_text(output_path, output + "\n")
        try:
            return parse_iperf3_json(output, direction)
        except (json.JSONDecodeError, ValueError) as error:
            raise CommandFailure(
                f"UE {ue_index} {direction} iperf3 failed: {error}"
            ) from error

    def start_background_ping(self, log_path, *, interval, count=None, deadline=None, ue_index=1):
        netns = self.ue_netns_for(ue_index)
        command = f"nohup ip netns exec {self.shell_quote(netns)} ping -D -i {float(interval):g}"
        if count is not None:
            command += f" -c {int(count)}"
        if deadline is not None:
            command += f" -W {deadline}"
        command += f" {self.shell_quote(self.gateway)} >{self.shell_quote(log_path)} 2>&1 </dev/null &"
        self.ue_capture(command)

    def stop_background_ping(self, *, interval):
        self.ue_capture(
            f"pkill -TERM -f '[p]ing -D -i {float(interval):g} .*{self.shell_quote(self.gateway)}' 2>/dev/null || true",
            check=False,
        )

    def capture_logs(self, trial_dir):
        logs = pathlib.Path(trial_dir) / "condition/logs"
        logs.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            logs / "gnuradio.log",
            self.ue_capture(
                f"cat {self.shell_quote(self.logs['gnuradio'])}"
            ) + "\n",
        )
        for ue_index in range(1, self.num_ues + 1):
            name = "ue.log" if self.num_ues == 1 else f"ue-{ue_index}.log"
            atomic_write_text(
                logs / name,
                self.ue_capture(
                    f"cat {self.shell_quote(self.ue_log_path(ue_index))}"
                ) + "\n",
            )
        atomic_write_text(
            logs / "gnb.log",
            self.gnb_capture(
                f"cat {self.shell_quote(self.logs['gnb'])}"
            ) + "\n",
        )
        monitoring = pathlib.Path(trial_dir) / "condition/monitoring"
        monitoring.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            monitoring / "gnb-metrics.jsonl",
            self.gnb_capture(
                f"cat {self.shell_quote(self.logs['gnb_metrics'])}",
            ) + "\n",
        )
        atomic_write_text(
            logs / "gnb-metrics-capture.log",
            self.gnb_capture(
                f"cat {self.shell_quote(self.logs['gnb_metrics_capture'])}",
            ) + "\n",
        )
        atomic_write_text(
            logs / "gnb-scheduler.log",
            self.gnb_capture(
                f"cat {self.shell_quote(self.logs['gnb_scheduler'])}",
            ) + "\n",
        )

    def restore(self, output_dir):
        if self.original is None:
            return
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.stop_radio()
        self.executor.run(self.kubectl("scale", f"deployment/{self.ue_deployment}", "-n", self.namespace, "--replicas=0"), output_dir / "scale-zero.log")
        self.wait_no_ue()
        self.executor.run(self.kubectl("apply", "-k", str(self.repo_root / self.baseline_overlay), "-n", self.namespace), output_dir / "apply-baseline.log")
        self.executor.run(self.kubectl("scale", f"deployment/{self.ue_deployment}", "-n", self.namespace, f"--replicas={self.original.replicas}"), output_dir / "scale-original.log")
        rollout = int(self.timeouts["rollout_seconds"])
        if self.original.replicas:
            self.executor.run(self.kubectl("rollout", "status", f"deployment/{self.ue_deployment}", "-n", self.namespace, f"--timeout={rollout}s"), output_dir / "rollout.log", timeout=rollout + 10)
            self.executor.run(self.kubectl("wait", "--for=condition=Ready", "pod", "-l", self.ue_selector, "-n", self.namespace, f"--timeout={rollout}s"), output_dir / "ready.log", timeout=rollout + 10)
        restored = self.current_state()
        write_json(output_dir / "restored-state.json", asdict(restored))
        def cm_base(name):
            return name.rsplit("-", 1)[0]
        restored_ok = (
            restored.replicas == self.original.replicas
            and restored.image == self.original.image
            and restored.pull_policy == self.original.pull_policy
            and cm_base(restored.configmap) == cm_base(self.original.configmap)
        )
        if not restored_ok:
            raise CommandFailure(f"restored deployment differs from original: {restored} != {self.original}")
        self.ue_pod = None
        self.gnb_pod = None

    def current_state(self):
        def value(expression):
            return self.capture("get", "deployment", self.ue_deployment, "-n", self.namespace, "-o", f"jsonpath={expression}")
        return OriginalUEState(
            replicas=int(value("{.spec.replicas}")),
            configmap=value(f'{{.spec.template.spec.volumes[?(@.name=="{self.ue_config_volume}")].configMap.name}}'),
            image=value(f'{{.spec.template.spec.containers[?(@.name=="{self.ue_container}")].image}}'),
            pull_policy=value(f'{{.spec.template.spec.containers[?(@.name=="{self.ue_container}")].imagePullPolicy}}'),
        )

    def baseline_environment(self):
        env = os.environ.copy()
        env.update({
            "NAMESPACE": self.namespace,
            "UE_NUMBER": str(self.num_ues),
            "UE_SELECTOR": self.ue_selector,
            "GNB_SELECTOR": self.gnb_selector,
            "UE_CONTAINER": self.ue_container,
            "GNB_CONTAINER": self.gnb_container,
            "GATEWAY": self.gateway,
            "TUN_INTERFACE": self.tun_interface,
            "GNURADIO_LOG": self.logs["gnuradio"],
            "GNB_LOG": self.logs["gnb"],
            "GNB_SCHEDULER_LOG": self.logs["gnb_scheduler"],
            "UE_LOG": self.logs["ue"],
            "START_GNU_SCRIPT": START_GNU_SCRIPT,
            "START_GNB_SCRIPT": START_GNB_SCRIPT,
            "START_UE_SCRIPT": START_UE_SCRIPT,
            "ATTACHMENT_LOG_PHRASE": self.radio["attachment_log_phrase"],
            "GNB_READY_LOG_PHRASE": self.radio["gnb_ready_log_phrase"],
            "GNURADIO_READY_LOG_PHRASE": self.radio[
                "gnuradio_ready_log_phrase"
            ],
            "UE_READY_LOG_PHRASE": self.radio["ue_ready_log_phrase"],
            "FLOWGRAPH_PROCESS_PATTERN": self.flowgraph_pattern,
            "UE_PROCESS_PATTERN": self.ue_process_pattern,
            "GNB_PROCESS_PATTERN": self.gnb_process_pattern,
            "WAIT_SECONDS": str(
                self.timeouts["baseline_attachment_seconds"]
            ),
        })
        return env

    def baseline_check(self, output_dir, *, ping_count=100, monitor_trial_dir=None):
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        monitor = None
        env = self.baseline_environment()
        script = str(self.repo_root / self.baseline_script)
        try:
            self.executor.run(
                [script, "stop"],
                output_dir / "reset.log",
                timeout=float(self.timeouts["baseline_start_seconds"]),
                env=env,
            )
            self.executor.run(
                [script, "start"],
                output_dir / "start.log",
                timeout=float(self.timeouts["baseline_start_seconds"]),
                env=env,
            )
            self.ue_pod = self.discover_ue()
            self.gnb_pod = self.discover_gnb()
            ue_ips = []
            for ue_index in range(1, self.num_ues + 1):
                netns = self.ue_netns_for(ue_index)
                ue_ip = self.ue_capture(
                    f"ip netns exec {self.shell_quote(netns)} "
                    "ip -4 -o addr show dev "
                    f"{self.shell_quote(self.tun_interface)} "
                    "| awk '{print $4}'"
                )
                ue_ips.append({
                    "ue_index": ue_index,
                    "ue_ip": ue_ip,
                })
            if monitor_trial_dir is not None:
                monitor = ResourceMonitor(
                    self,
                    monitor_trial_dir,
                    self.parameters["monitoring"][
                        "process_interval_seconds"
                    ],
                )
                monitor.start()
            pings = []
            for ue_index in range(1, self.num_ues + 1):
                name = (
                    "ping.txt" if self.num_ues == 1
                    else f"ping-ue{ue_index}.txt"
                )
                ping = self.ping(
                    output_dir / name,
                    count=ping_count,
                    interval=self.channel["baseline_ping_interval_seconds"],
                    deadline=self.channel["baseline_ping_deadline_seconds"],
                    ue_index=ue_index,
                )
                pings.append({"ue_index": ue_index, "ping": ping})
            if monitor is not None:
                monitor.check()
            self.executor.run([script, "logs"], output_dir / "logs.txt", check=False, env=env)
            return {
                "status": (
                    "passed"
                    if all(
                        item["ping"]["packet_loss_percent"] == 0.0
                        for item in pings
                    )
                    else "failed"
                ),
                "attachment_success": True,
                "ue_ips": ue_ips,
                "pings": pings,
            }
        finally:
            if monitor is not None:
                monitor.stop()
            self.executor.run([script, "stop"], output_dir / "stop.log", check=False, env=env)
