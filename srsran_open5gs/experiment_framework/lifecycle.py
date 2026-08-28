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


DEFAULT_LOGS = {
    "calibration_ping": "/tmp/evaluation-calibration-ping.log",
    "continuous_ping": "/tmp/evaluation-continuous-ping.log",
    "gnb": "/tmp/evaluation-gnb.log",
    "gnuradio": "/tmp/evaluation-gnuradio.log",
    "ue": "/tmp/evaluation-ue.log",
}


class CommandFailure(RuntimeError):
    def __init__(self, message, command=None, return_code=None):
        super().__init__(message)
        self.command = command
        self.return_code = return_code


def command_environment(env=None):
    result = dict(os.environ if env is None else env)
    result.pop("DEBUG", None)
    return result


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
        self.selector = parameters.get("kubernetes", {}).get("amf_selector")
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
        if self.selector:
            command.extend(["--selector", self.selector])
        safety = self.parameters.get("amf_safety", {})
        if "stop_at_growth_bytes" in safety:
            command.extend(["--stop-growth-bytes", str(safety["stop_at_growth_bytes"])])
        if "warn_at_growth_bytes" in safety:
            command.extend(["--warn-growth-bytes", str(safety["warn_at_growth_bytes"])])
        if "stop_at_limit_fraction" in safety:
            command.extend(["--stop-limit-fraction", str(safety["stop_at_limit_fraction"])])
        if "warn_at_limit_fraction" in safety:
            command.extend(["--warn-limit-fraction", str(safety["warn_at_limit_fraction"])])
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
                reason = summary.get("reason") or reason
            except (OSError, json.JSONDecodeError):
                pass
        raise SafetyStop(reason)

    def samples(self):
        values = []
        if not self.samples_path.exists():
            return values
        for line in self.samples_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
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
        monitor_config = self.lifecycle.parameters.get("monitoring", {})
        if monitor_config.get("enable_gpu", True):
            self.gpu = BackgroundCommand(
                [
                    monitor_config.get("nvidia_smi", "nvidia-smi"),
                    "--query-gpu=timestamp,index,uuid,utilization.gpu,utilization.memory,memory.used,power.draw,temperature.gpu",
                    "--format=csv",
                    "-lms", str(monitor_config.get("gpu_query_interval_ms", 1000)),
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
        self.kubernetes = parameters.get("kubernetes", {})
        self.radio = parameters.get("radio", {})
        self.logs = {**DEFAULT_LOGS, **parameters.get("logs", {})}
        self.timeouts = parameters.get("timeouts", {})
        self.original = None
        self.ue_pod = None
        self.gnb_pod = None
        self.ue_selector = self.kubernetes.get("ue_selector")
        self.gnb_selector = self.kubernetes.get("gnb_selector")
        self.ue_deployment = self.kubernetes.get("ue_deployment")
        self.ue_container = self.kubernetes.get("ue_container", "ue")
        self.gnb_container = self.kubernetes.get("gnb_container", "gnb")
        self.ue_config_volume = self.kubernetes.get("ue_config_volume", "ue-volume")
        self.baseline_overlay = self.kubernetes.get("baseline_overlay", "configs/ues/srsue")
        self.baseline_script = self.kubernetes.get("baseline_script", "bin/baseline.sh")
        self.num_ues = int(self.radio.get("ue_number", 1))
        self.ue_netns = self.radio.get("ue_netns", "ue1")
        self.secondary_interface = self.radio.get(
            "secondary_interface", "n3"
        )
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

    def capture(self, *arguments, check=True):
        return self.executor.capture(self.kubectl(*arguments), check=check)

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

        required = self.parameters.get("runtime_images", {}).get(
            "required_in_kubernetes", []
        )
        nodes = json.loads(self.capture("get", "nodes", "-o", "json"))
        images = {
            name
            for node in nodes.get("items", [])
            for image in node.get("status", {}).get("images", [])
            for name in image.get("names", [])
        }
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
            "failures": failures,
        }
        write_json(output_dir / "integration-preflight.json", report)
        if failures:
            raise CommandFailure(
                "integration preflight failed: " + "; ".join(failures)
            )
        return report

    def ue_capture(self, script, check=True):
        self.ue_pod = self.ue_pod or self.discover_ue()
        return self.capture("exec", "-n", self.namespace, self.ue_pod, "-c", self.ue_container, "--", "bash", "-lc", script, check=check)

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
        stop_sleep = float(self.timeouts.get("radio_stop_sleep_seconds", 2.0))
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
                "bash", "-lc", f"pkill -INT -f {self.shell_quote(self.gnb_process_pattern)} 2>/dev/null || true",
                check=False,
            )
        time.sleep(stop_sleep)
        if self.ue_pod:
            self.capture(
                "exec", "-n", self.namespace, self.ue_pod, "-c", self.ue_container, "--",
                "bash", "-lc",
                f"pkill -INT -f {self.shell_quote(self.flowgraph_pattern)} 2>/dev/null || true; "
                "pkill -TERM -f '[t]ail -f /dev/null' 2>/dev/null || true",
                check=False,
            )

    def wait_no_ue(self, timeout=None):
        timeout = float(timeout or self.timeouts.get("ue_wait_gone_seconds", 180))
        # srsUE ignores SIGTERM; use forced deletion.
        force_after = float(self.timeouts.get("ue_force_delete_after_seconds", 15))
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
        rollout = int(self.timeouts.get("rollout_seconds", 300))
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
        wrapper = self.ue_capture(f"pgrep -af {self.shell_quote(self.flowgraph_pattern + '|' + self.ue_process_pattern)} || true")
        atomic_write_text(output_dir / "wrapper-process-check.txt", wrapper + ("\n" if wrapper else ""))
        if wrapper:
            raise CommandFailure("replacement UE pod started radio processes automatically")

    def start_radio(self, condition, trial_dir):
        trial_dir = pathlib.Path(trial_dir)
        launcher = condition["launcher"]
        gnuradio_log = self.shell_quote(self.logs["gnuradio"])
        gnb_log = self.shell_quote(self.logs["gnb"])
        self.ue_capture(f"nohup {launcher} >{gnuradio_log} 2>&1 </dev/null &")
        time.sleep(float(self.timeouts.get("radio_start_gnuradio_sleep_seconds", 5.0)))
        flow = self.ue_capture(f"pgrep -f {self.shell_quote(self.flowgraph_pattern)} | head -n1")
        if not flow:
            raise CommandFailure("GNU Radio process did not remain running")
        self.gnb_capture(f"nohup {START_GNB_SCRIPT} >{gnb_log} 2>&1 </dev/null &")
        time.sleep(float(self.timeouts.get("radio_start_gnb_sleep_seconds", 3.0)))
        timeout = condition["measurement_profile_resolved"]["values"].get("attachment_timeout_seconds", 120)
        phrase = self.shell_quote(self.radio.get("attachment_log_phrase", "PDU Session Establishment successful"))
        ue_ips = []
        for ue_index in range(1, self.num_ues + 1):
            ue_ips.append(self._start_one_ue(ue_index, trial_dir, timeout, phrase))
        return ue_ips

    def _start_one_ue(self, ue_index, trial_dir, timeout, phrase):
        ue_log = self.shell_quote(self.ue_log_path(ue_index))
        netns = self.ue_netns_for(ue_index)
        self.ue_capture(f"nohup {START_UE_SCRIPT} {ue_index} >{ue_log} 2>&1 </dev/null &")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ue_capture(f"grep -Fq {phrase} {ue_log}; printf '%s' $?", check=False) == "0":
                break
            if not self.ue_capture(f"pgrep -f {self.shell_quote(self.ue_process_pattern)} || true"):
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

    def ping(self, output_path, *, count=100, interval=0.1, deadline=2, ue_index=1):
        netns = self.ue_netns_for(ue_index)
        output = self.ue_capture(
            f"ip netns exec {self.shell_quote(netns)} ping -D -i {float(interval):g} -c {int(count)} -W {deadline} {self.shell_quote(self.gateway)}",
            check=False,
        )
        atomic_write_text(output_path, output + "\n")
        return parse_ping(output)

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
        atomic_write_text(logs / "gnuradio.log", self.ue_capture(f"cat {self.shell_quote(self.logs['gnuradio'])} 2>/dev/null || true", check=False) + "\n")
        for ue_index in range(1, self.num_ues + 1):
            name = "ue.log" if self.num_ues == 1 else f"ue-{ue_index}.log"
            atomic_write_text(logs / name, self.ue_capture(f"cat {self.shell_quote(self.ue_log_path(ue_index))} 2>/dev/null || true", check=False) + "\n")
        atomic_write_text(logs / "gnb.log", self.gnb_capture(f"cat {self.shell_quote(self.logs['gnb'])} 2>/dev/null || true", check=False) + "\n")

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
        rollout = int(self.timeouts.get("rollout_seconds", 300))
        if self.original.replicas:
            self.executor.run(self.kubectl("rollout", "status", f"deployment/{self.ue_deployment}", "-n", self.namespace, f"--timeout={rollout}s"), output_dir / "rollout.log", timeout=rollout + 10)
            self.executor.run(self.kubectl("wait", "--for=condition=Ready", "pod", "-l", self.ue_selector, "-n", self.namespace, f"--timeout={rollout}s"), output_dir / "ready.log", timeout=rollout + 10)
        restored = self.current_state()
        write_json(output_dir / "restored-state.json", asdict(restored))
        # Kustomize appends a hash to ConfigMap names.
        def cm_base(name):
            return name.rsplit("-", 1)[0] if name else name
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
            "UE_NETNS": self.ue_netns,
            "GATEWAY": self.gateway,
            "TUN_INTERFACE": self.tun_interface,
            "GNURADIO_LOG": self.logs["gnuradio"],
            "GNB_LOG": self.logs["gnb"],
            "UE_LOG": self.logs["ue"],
            "START_GNU_SCRIPT": START_GNU_SCRIPT,
            "START_GNB_SCRIPT": START_GNB_SCRIPT,
            "START_UE_SCRIPT": START_UE_SCRIPT,
            "ATTACHMENT_LOG_PHRASE": self.radio.get("attachment_log_phrase", "PDU Session Establishment successful"),
            "FLOWGRAPH_PROCESS_PATTERN": self.flowgraph_pattern,
            "UE_PROCESS_PATTERN": self.ue_process_pattern,
            "GNB_PROCESS_PATTERN": self.gnb_process_pattern,
        })
        return env

    def throughput_record(self):
        return {
            "status": "deferred",
            "reason": "No verified user-plane throughput endpoint exists",
        }

    def baseline_check(self, output_dir, *, ping_count=100, monitor_trial_dir=None):
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        monitor = None
        env = self.baseline_environment()
        script = str(self.repo_root / self.baseline_script)
        try:
            self.executor.run([script, "start"], output_dir / "start.log", timeout=float(self.timeouts.get("baseline_start_seconds", 180)), env=env)
            self.ue_pod = self.discover_ue()
            self.gnb_pod = self.discover_gnb()
            ue_ip = self.ue_capture(f"ip netns exec {self.shell_quote(self.ue_netns)} ip -4 -o addr show dev {self.shell_quote(self.tun_interface)} | awk '{{print $4}}'")
            if monitor_trial_dir is not None:
                monitor = ResourceMonitor(self, monitor_trial_dir, self.parameters.get("monitoring", {}).get("process_interval_seconds", 1.0))
                monitor.start()
            ping = self.ping(output_dir / "ping.txt", count=ping_count)
            if monitor is not None:
                monitor.check()
            self.executor.run([script, "logs"], output_dir / "logs.txt", check=False, env=env)
            return {
                "status": "passed" if ping["packet_loss_percent"] == 0.0 else "failed",
                "attachment_success": True,
                "ue_ip": ue_ip,
                "ping": ping,
                "throughput": self.throughput_record(),
            }
        finally:
            if monitor is not None:
                monitor.stop()
            self.executor.run([script, "stop"], output_dir / "stop.log", check=False, env=env)
