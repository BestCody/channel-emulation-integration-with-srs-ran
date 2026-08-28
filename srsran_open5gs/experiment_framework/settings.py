#!/usr/bin/env python3

import copy
import json
import os
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


def _set_nested(parameters, keys, value):
    current = parameters
    for key in keys[:-1]:
        current = current.setdefault(key, {})
    current[keys[-1]] = value


def _bool(value):
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value}")


ENVIRONMENT_OVERRIDES = {
    "SRSRAN_UE_NUMBER": (("radio", "ue_number"), int),
    "SIONNA_PYTHON": (("host_python",), str),
    "BENCHMARK_RESULT_ROOT": (("result_root",), str),
    "SIONNA_RANDOMIZE_POSITIONS": (("scene", "randomize_positions"), _bool),
    "SIONNA_PLACEMENT_SEED": (("scene", "placement_seed"), int),
}


def load_benchmark_parameters(*parameter_files, inline=None):
    parameters = {}
    sources = []
    if DEFAULT_PARAMETER_FILE.exists():
        parameters = _deep_merge(parameters, _load_json(DEFAULT_PARAMETER_FILE))
        sources.append(str(DEFAULT_PARAMETER_FILE))
    for path in parameter_files:
        if path:
            parameters = _deep_merge(parameters, _load_json(path))
            sources.append(str(pathlib.Path(path).resolve()))
    if inline:
        if not isinstance(inline, dict):
            raise ValueError("inline parameters must be a JSON object")
        parameters = _deep_merge(parameters, inline)
    for name, (keys, converter) in ENVIRONMENT_OVERRIDES.items():
        if name in os.environ:
            _set_nested(parameters, keys, converter(os.environ[name]))
    parameters.setdefault("host_python", sys.executable)
    parameters["_parameter_sources"] = sources
    return parameters


def resolve_repo_path(value, *, repo_root=REPO_ROOT):
    path = pathlib.Path(str(value)).expanduser()
    if not path.is_absolute():
        path = pathlib.Path(repo_root) / path
    return path.resolve()


def parameter_sources(parameters):
    return tuple(parameters.get("_parameter_sources", ()))
