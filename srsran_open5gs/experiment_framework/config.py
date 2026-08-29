#!/usr/bin/env python3

import copy
import hashlib
import json
import math
import pathlib
from datetime import datetime, timezone

from .settings import _deep_merge, load_benchmark_parameters, parameter_sources, resolve_repo_path


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROPAGATION_EFFECTS = (
    "los",
    "specular_reflection",
    "diffuse_reflection",
    "refraction",
    "diffraction",
    "edge_diffraction",
    "diffraction_lit_region",
)
SOLVER_TUNING_KEYS = {
    "max_depth",
    "max_num_paths_per_src",
    "samples_per_src",
    "synthetic_array",
    "seed",
}
SOLVER_KEYS = set(PROPAGATION_EFFECTS) | SOLVER_TUNING_KEYS
CONDITION_FIELDS = {
    "schema_version",
    "condition_id",
    "label",
    "description",
    "launcher",
    "scene",
    "scene_overrides",
    "placement_mode",
    "placement_seed",
    "propagation",
    "noise",
    "trajectory",
    "measurement_profile",
    "overlay",
}
STUDY_FIELDS = {
    "schema_version",
    "study_id",
    "description",
    "pilot",
    "parameters",
    "parameter_files",
    "conditions",
    "trials_per_condition",
    "amf_safety",
}


class ConfigError(ValueError):
    pass


def apply_propagation(scene, propagation):
    merged = copy.deepcopy(scene)
    solver = dict(merged["solver"])
    for effect in PROPAGATION_EFFECTS:
        solver[effect] = False
    solver.update(propagation)
    merged["solver"] = solver
    return merged


def sha256_file(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    path = pathlib.Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"could not read JSON configuration {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"configuration must be a JSON object: {path}")
    return value


def parse_overrides(items):
    result = {}
    for item in items or []:
        key, separator, raw = str(item).partition("=")
        key = key.strip()
        if not separator or not key:
            raise ConfigError(f"override must be KEY=VALUE: {item!r}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        node = result
        parts = key.split(".")
        for part in parts[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[parts[-1]] = value
    return result


def source_path(value, *, relative_to=None):
    path = pathlib.Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (relative_to or REPO_ROOT) / path
    path = path.resolve()
    if not path.exists():
        raise ConfigError(f"referenced file does not exist: {path}")
    return path


def source_record(path):
    path = pathlib.Path(path).resolve()
    try:
        display = str(path.relative_to(REPO_ROOT))
    except ValueError:
        display = str(path)
    return {
        "path": display,
        "absolute_path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _format_launcher(condition, parameters):
    launcher = condition["launcher"]
    values = {
        "ue_number": parameters["radio"]["ue_number"],
    }
    try:
        condition["launcher"] = str(launcher).format(**values)
    except KeyError as error:
        raise ConfigError(f"unknown launcher parameter {error} in {condition['condition_id']}") from error


def validate_condition(condition, condition_path, parameters):
    if condition.get("schema_version") != 1:
        raise ConfigError(f"unsupported condition schema: {condition_path}")
    condition_id = condition.get("condition_id")
    if not isinstance(condition_id, str) or not condition_id:
        raise ConfigError("condition_id is required")
    for field in (
        "launcher",
        "scene",
        "trajectory",
        "measurement_profile",
        "overlay",
    ):
        if not isinstance(condition.get(field), str) or not condition[field]:
            raise ConfigError(
                f"condition {condition_id} requires {field}"
            )
    unknown_fields = set(condition) - CONDITION_FIELDS
    if unknown_fields:
        raise ConfigError(
            f"condition {condition_id} has unknown fields: "
            f"{sorted(unknown_fields)}"
        )

    propagation = condition.get("propagation")
    if not isinstance(propagation, dict):
        raise ConfigError(f"condition {condition_id} propagation must be an object")
    unknown = set(propagation) - SOLVER_KEYS
    if unknown:
        raise ConfigError(f"condition {condition_id} has unknown propagation keys: {sorted(unknown)}")

    if condition.get("placement_mode") not in {"configured", "random"}:
        raise ConfigError(
            f"condition {condition_id} requires a valid placement_mode"
        )

    noise = condition.get("noise", {})
    if not isinstance(noise, dict):
        raise ConfigError(f"condition {condition_id} noise must be an object")
    unknown_noise = set(noise) - {"sigma", "snr_db"}
    if unknown_noise:
        raise ConfigError(
            f"condition {condition_id} has unknown noise keys: "
            f"{sorted(unknown_noise)}"
        )
    if "sigma" in noise and "snr_db" in noise:
        raise ConfigError(
            f"condition {condition_id} cannot set sigma and snr_db"
        )
    for key, value in noise.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"condition {condition_id} noise {key} must be numeric"
            )
        if not math.isfinite(float(value)):
            raise ConfigError(
                f"condition {condition_id} noise {key} must be finite"
            )
    if "sigma" in noise and float(noise["sigma"]) < 0.0:
        raise ConfigError(
            f"condition {condition_id} noise sigma cannot be negative"
        )
    if "sigma" in noise and float(noise["sigma"]) > 10.0:
        raise ConfigError(
            f"condition {condition_id} noise sigma cannot exceed 10"
        )

    _format_launcher(condition, parameters)
    return condition


def add_artifact(condition, key, artifacts):
    value = condition.get(key)
    if value is None:
        return
    path = source_path(value, relative_to=REPO_ROOT)
    record = source_record(path)
    condition[f"{key}_resolved"] = record
    artifacts.append(record)


def resolve_condition(
    reference,
    study_path,
    parameters,
    condition_overrides=None,
    scene_overrides=None,
    profile_overrides=None,
):
    condition_path = source_path(reference, relative_to=study_path.parent)
    raw = load_json(condition_path)
    if condition_overrides:
        raw = _deep_merge(raw, condition_overrides)
    condition = validate_condition(raw, condition_path, parameters)
    resolved = copy.deepcopy(condition)
    resolved["configuration"] = source_record(condition_path)
    if scene_overrides:
        resolved["scene_overrides"] = copy.deepcopy(scene_overrides)

    profile_path = source_path(
        condition["measurement_profile"],
        relative_to=condition_path.parent,
    )
    profile = load_json(profile_path)
    if profile_overrides:
        profile = _deep_merge(profile, profile_overrides)
    if profile.get("schema_version") != 1:
        raise ConfigError(f"unsupported measurement profile: {profile_path}")
    required_profile = {
        "attachment_timeout_seconds",
        "final_ping",
        "throughput",
        "amf_interval_seconds",
        "resource_interval_seconds",
    }
    missing_profile = required_profile - set(profile)
    if missing_profile:
        raise ConfigError(
            f"measurement profile is missing {sorted(missing_profile)}"
        )
    unknown_profile = set(profile) - required_profile - {
        "schema_version",
        "description",
    }
    if unknown_profile:
        raise ConfigError(
            f"measurement profile has unknown fields: "
            f"{sorted(unknown_profile)}"
        )
    final_ping = profile["final_ping"]
    if not isinstance(final_ping, dict) or set(final_ping) != {
        "count",
        "deadline_seconds",
        "interval_seconds",
    }:
        raise ConfigError(
            "measurement final_ping requires count, deadline_seconds, "
            "and interval_seconds"
        )
    if (
        isinstance(final_ping["count"], bool)
        or not isinstance(final_ping["count"], int)
        or final_ping["count"] < 1
    ):
        raise ConfigError("final ping count must be a positive integer")
    for key in (
        "attachment_timeout_seconds",
        "amf_interval_seconds",
        "resource_interval_seconds",
    ):
        value = profile[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ConfigError(f"measurement {key} must be positive")
    for key in ("deadline_seconds", "interval_seconds"):
        value = final_ping[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ConfigError(f"final ping {key} must be positive")
    throughput = profile["throughput"]
    if not isinstance(throughput, dict):
        raise ConfigError("measurement throughput must be an object")
    required_throughput = {
        "enabled",
        "duration_seconds",
        "omit_seconds",
        "connect_timeout_seconds",
        "completion_timeout_seconds",
    }
    missing_throughput = required_throughput - set(throughput)
    if missing_throughput:
        raise ConfigError(
            "measurement throughput is missing "
            f"{sorted(missing_throughput)}"
        )
    enabled = throughput["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigError("measurement throughput enabled must be boolean")
    duration = throughput["duration_seconds"]
    omit = throughput["omit_seconds"]
    connect_timeout = throughput["connect_timeout_seconds"]
    completion_timeout = throughput["completion_timeout_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) <= 0.0
    ):
        raise ConfigError("throughput duration must be finite and positive")
    if (
        isinstance(omit, bool)
        or not isinstance(omit, (int, float))
        or not math.isfinite(float(omit))
        or float(omit) < 0.0
    ):
        raise ConfigError("throughput omit must be finite and non-negative")
    if (
        isinstance(connect_timeout, bool)
        or not isinstance(connect_timeout, (int, float))
        or not math.isfinite(float(connect_timeout))
        or float(connect_timeout) <= 0.0
    ):
        raise ConfigError(
            "throughput connect timeout must be finite and positive"
        )
    if (
        isinstance(completion_timeout, bool)
        or not isinstance(completion_timeout, (int, float))
        or not math.isfinite(float(completion_timeout))
        or float(completion_timeout) <= float(connect_timeout)
        or float(completion_timeout) <= float(duration) + float(omit)
    ):
        raise ConfigError(
            "throughput completion timeout must exceed connection "
            "and transfer timeouts"
        )
    resolved["measurement_profile_resolved"] = {
        "configuration": source_record(profile_path),
        "values": profile,
    }

    artifacts = [resolved["configuration"], source_record(profile_path)]
    add_artifact(resolved, "scene", artifacts)
    add_artifact(resolved, "trajectory", artifacts)
    resolved["input_artifacts"] = artifacts
    return resolved


def _result_root(study, parameters):
    return resolve_repo_path(parameters["result_root"], repo_root=REPO_ROOT)


def validate_study(study, study_path, parameters):
    if study.get("schema_version") != 1:
        raise ConfigError("unsupported study schema")
    if not study.get("study_id"):
        raise ConfigError("study_id is required")
    unknown_fields = set(study) - STUDY_FIELDS
    if unknown_fields:
        raise ConfigError(
            f"study has unknown fields: {sorted(unknown_fields)}"
        )
    if not isinstance(study.get("pilot"), bool):
        raise ConfigError("study pilot must be boolean")
    result_root = _result_root(study, parameters)
    if parameters["results_must_be_outside_repo"]:
        if REPO_ROOT == result_root or REPO_ROOT in result_root.parents:
            raise ConfigError("generated results must be outside the Git repository")
    references = study.get("conditions")
    if not isinstance(references, list) or not references:
        raise ConfigError("study conditions must be a non-empty list")
    trials = study.get("trials_per_condition")
    if (
        not isinstance(trials, int)
        or isinstance(trials, bool)
        or not 1 <= trials <= 31
    ):
        raise ConfigError("trials_per_condition must be in 1..31")
    if study["pilot"] and trials != 1:
        raise ConfigError("pilot trial count does not match benchmark parameters")
    safety = study.get("amf_safety")
    if not isinstance(safety, dict):
        raise ConfigError("amf_safety must be an object")
    required_safety = {
        "stop_at_growth_bytes",
        "warn_at_growth_bytes",
        "stop_at_limit_fraction",
        "warn_at_limit_fraction",
    }
    if set(safety) != required_safety:
        raise ConfigError(
            "amf_safety must contain exactly "
            f"{sorted(required_safety)}"
        )
    for key, value in safety.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ConfigError(f"AMF safety {key} must be numeric")
    if not 0 <= safety["warn_at_growth_bytes"] < safety["stop_at_growth_bytes"]:
        raise ConfigError("AMF memory growth thresholds are invalid")
    if not 0.0 <= safety["warn_at_limit_fraction"] < safety["stop_at_limit_fraction"] <= 1.0:
        raise ConfigError("AMF memory limit thresholds are invalid")
    return result_root


def _parameter_files(study, study_path, cli_parameter_files):
    files = []
    for item in study.get("parameter_files", []):
        files.append(source_path(item, relative_to=study_path.parent))
    for item in cli_parameter_files or []:
        files.append(source_path(item, relative_to=pathlib.Path.cwd()))
    return files


def load_and_resolve_study(
    path,
    *,
    resolved_at=None,
    parameter_files=None,
    parameter_overrides=None,
    condition_overrides=None,
    scene_overrides=None,
    profile_overrides=None,
):
    study_path = pathlib.Path(path).resolve()
    study = load_json(study_path)
    if parameter_overrides:
        for key, value in parameter_overrides.items():
            if key in study:
                study[key] = (
                    _deep_merge(study[key], value)
                    if isinstance(study.get(key), dict) and isinstance(value, dict)
                    else value
                )
    files = _parameter_files(study, study_path, parameter_files)
    inline = study.get("parameters") or {}
    if parameter_overrides:
        inline = _deep_merge(inline, parameter_overrides)
    try:
        parameters = load_benchmark_parameters(*files, inline=inline)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ConfigError(f"could not load benchmark parameters: {error}") from error
    parameters["amf_safety"] = copy.deepcopy(study["amf_safety"])
    result_root = validate_study(study, study_path, parameters)
    conditions = [
        resolve_condition(
            item, study_path, parameters,
            condition_overrides, scene_overrides, profile_overrides,
        )
        for item in study["conditions"]
    ]
    identifiers = [item["condition_id"] for item in conditions]
    if len(identifiers) != len(set(identifiers)):
        raise ConfigError("condition identifiers must be unique")

    timestamp = resolved_at or datetime.now(timezone.utc).isoformat()
    resolved = copy.deepcopy(study)
    resolved["resolved_at_utc"] = timestamp
    resolved["study_configuration"] = source_record(study_path)
    resolved["parameter_configurations"] = [
        source_record(item) for item in parameter_sources(parameters)
    ]
    resolved["parameters"] = parameters
    resolved["result_root"] = str(result_root)
    resolved["conditions"] = conditions
    resolved["trial_count"] = len(conditions) * study["trials_per_condition"]
    resolved["cli_overrides"] = {
        "parameters": copy.deepcopy(parameter_overrides or {}),
        "conditions": copy.deepcopy(condition_overrides or {}),
        "scene": copy.deepcopy(scene_overrides or {}),
        "profile": copy.deepcopy(profile_overrides or {}),
    }
    return resolved
