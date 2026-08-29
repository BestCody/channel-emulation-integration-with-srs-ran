#!/usr/bin/env python3

import copy
import json
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PARAMETER_FILE = REPO_ROOT / "experiments" / "benchmark-parameters.json"

CONTROL_ENDPOINT = "tcp://127.0.0.1:5555"
STREAM_ENDPOINT = "tcp://127.0.0.1:5556"
PORT_FORWARD = "5555:5555"
PORT_FORWARD_STREAM = "5556:5556"
PORT_FORWARD_HOST = "127.0.0.1"
PORT_FORWARD_PORT = 5555
FLOWGRAPH_PROCESS_PATTERN = "[m]ulti_ue_.*channel.py|[m]ulti_ue_scenario.py"
GNB_PROCESS_PATTERN = "[/]srsran/gnb"
UE_PROCESS_PATTERN = "[/]opt/srsRAN_4G/build/srsue/src/srsue"
START_GNB_SCRIPT = "/srsran/config/start_gnb.sh"
START_GNU_SCRIPT = "/srsran/config/start_gnu.sh"
START_UE_SCRIPT = "/srsran/config/start_ue.sh"
TUN_INTERFACE = "tun_srsue"
GATEWAY = "10.41.0.1"
PARAMETER_FIELDS = {
    "channel": {
        "baseline_ping_deadline_seconds",
        "baseline_ping_interval_seconds",
        "control_timeout_ms",
        "continuous_ping_interval_seconds",
        "final_hold_seconds",
        "interpolation_steps",
        "moving_live_timeout_seconds",
        "port_forward_ready_seconds",
    },
    "kubernetes": {
        "amf_selector",
        "baseline_overlay",
        "baseline_script",
        "gnb_container",
        "gnb_selector",
        "iperf_container",
        "iperf_port",
        "ue_config_volume",
        "ue_container",
        "ue_deployment",
        "ue_selector",
        "upf_selector",
    },
    "radio": {
        "attachment_log_phrase",
        "gnb_ready_log_phrase",
        "gnuradio_ready_log_phrase",
        "metrics_bind",
        "metrics_port",
        "secondary_interface",
        "ue_ready_log_phrase",
        "ue_number",
    },
    "logs": {
        "continuous_ping",
        "gnb",
        "gnb_metrics",
        "gnb_metrics_capture",
        "gnb_scheduler",
        "gnuradio",
        "ue",
    },
    "monitoring": {
        "gpu_query_interval_ms",
        "nvidia_smi",
        "process_interval_seconds",
    },
    "runtime_images": {"required_in_kubernetes"},
    "scene": {"min_link_distance_m", "placement_seed"},
    "timeouts": {
        "baseline_attachment_seconds",
        "baseline_start_seconds",
        "radio_start_gnuradio_sleep_seconds",
        "radio_stop_sleep_seconds",
        "rollout_seconds",
        "ue_force_delete_after_seconds",
        "ue_wait_gone_seconds",
    },
}
PARAMETER_SCALARS = {"result_root", "results_must_be_outside_repo"}


def _deep_merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_json(path):
    path = pathlib.Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"parameter file must contain a JSON object: {path}")
    return value


def _validate_parameters(parameters):
    expected = set(PARAMETER_FIELDS) | PARAMETER_SCALARS
    if set(parameters) != expected:
        raise ValueError(
            "benchmark parameters must contain exactly "
            f"{sorted(expected)}"
        )
    for section, fields in PARAMETER_FIELDS.items():
        value = parameters[section]
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(
                f"benchmark section {section} must contain exactly "
                f"{sorted(fields)}"
            )


def load_benchmark_parameters(*parameter_files, inline=None):
    parameters = _load_json(DEFAULT_PARAMETER_FILE)
    sources = [str(DEFAULT_PARAMETER_FILE)]
    for path in parameter_files:
        if path:
            parameters = _deep_merge(parameters, _load_json(path))
            sources.append(str(pathlib.Path(path).resolve()))
    if inline:
        if not isinstance(inline, dict):
            raise ValueError("inline parameters must be a JSON object")
        parameters = _deep_merge(parameters, inline)
    _validate_parameters(parameters)
    parameters["host_python"] = sys.executable
    parameters["_parameter_sources"] = sources
    return parameters


def resolve_repo_path(value, *, repo_root=REPO_ROOT):
    path = pathlib.Path(str(value)).expanduser()
    if not path.is_absolute():
        path = pathlib.Path(repo_root) / path
    return path.resolve()


def parameter_sources(parameters):
    return tuple(parameters["_parameter_sources"])
