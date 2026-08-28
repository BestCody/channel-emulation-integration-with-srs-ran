#!/usr/bin/env python3

import copy
import hashlib
import json
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
DEFERRED_THROUGHPUT = {
    "status": "deferred",
    "reason": "No verified user-plane throughput endpoint exists",
}


class ConfigError(ValueError):
    pass


def apply_propagation(scene, propagation):
    """Apply propagation toggles."""
    merged = copy.deepcopy(scene)
    solver = dict(merged.get("solver", {}))
    for effect in PROPAGATION_EFFECTS:
        solver[effect] = False
    solver.update(propagation or {})
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
    """Parse dotted command-line overrides."""
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


THROUGHPUT_STATUSES = {"deferred"}


def validate_throughput(condition):
    throughput = condition.get("throughput")
    if not isinstance(throughput, dict):
        raise ConfigError("every condition requires a throughput object")
    if throughput.get("status") not in THROUGHPUT_STATUSES:
        raise ConfigError(
            f"throughput status {throughput.get('status')!r} is not supported; "
            f"allowed: {sorted(THROUGHPUT_STATUSES)}"
        )


def _format_launcher(condition, parameters):
    launcher = condition.get("launcher")
    if launcher is None:
        return
    values = {
        "ue_number": parameters.get("radio", {}).get("ue_number", 1),
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
    if not condition.get("scene"):
        raise ConfigError(f"condition {condition_id} requires a scene")

    propagation = condition.get("propagation", {})
    if not isinstance(propagation, dict):
        raise ConfigError(f"condition {condition_id} propagation must be an object")
    unknown = set(propagation) - SOLVER_KEYS
    if unknown:
        raise ConfigError(f"condition {condition_id} has unknown propagation keys: {sorted(unknown)}")

    if not condition.get("trajectory"):
        raise ConfigError(f"condition {condition_id} requires a trajectory")

    validate_throughput(condition)
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


def add_nested_artifact(condition, keys, resolved_key, artifacts):
    value = condition
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    if value is None:
        return
    path = source_path(value, relative_to=REPO_ROOT)
    record = source_record(path)
    condition[resolved_key] = record
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
        condition.get("measurement_profile"),
        relative_to=condition_path.parent,
    )
    profile = load_json(profile_path)
    if profile_overrides:
        profile = _deep_merge(profile, profile_overrides)
    if profile.get("schema_version") != 1:
        raise ConfigError(f"unsupported measurement profile: {profile_path}")
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
    raw = study.get("result_root") or parameters.get("result_root")
    if not raw:
        raise ConfigError("result_root must be provided by the study or benchmark parameters")
    return resolve_repo_path(raw, repo_root=REPO_ROOT)


def validate_study(study, study_path, parameters):
    if study.get("schema_version") != 1:
        raise ConfigError("unsupported study schema")
    if not study.get("study_id"):
        raise ConfigError("study_id is required")
    result_root = _result_root(study, parameters)
    if parameters.get("results_must_be_outside_repo", True):
        if REPO_ROOT == result_root or REPO_ROOT in result_root.parents:
            raise ConfigError("generated results must be outside the Git repository")
    references = study.get("conditions")
    if not isinstance(references, list):
        raise ConfigError("study conditions must be a list")
    trials = study.get("trials_per_condition")
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 0:
        raise ConfigError("trials_per_condition must be a non-negative integer")
    if references and trials < 1:
        raise ConfigError("trials_per_condition must be positive when conditions are configured")
    if not references and trials != 0:
        raise ConfigError("trials_per_condition must be zero when no conditions are configured")
    study_policy = parameters.get("study", {})
    if study.get("pilot") and study_policy.get("enforce_pilot_single_trial") and trials != 1:
        raise ConfigError("pilot trial count does not match benchmark parameters")
    if not isinstance(study.get("baseline_policy", {}), dict):
        raise ConfigError("baseline_policy must be an object")
    if not isinstance(study.get("amf_safety", {}), dict):
        raise ConfigError("amf_safety must be an object")
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
    if isinstance(study.get("amf_safety"), dict):
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
    resolved["throughput"] = dict(DEFERRED_THROUGHPUT)
    resolved["cli_overrides"] = {
        "parameters": copy.deepcopy(parameter_overrides or {}),
        "conditions": copy.deepcopy(condition_overrides or {}),
        "scene": copy.deepcopy(scene_overrides or {}),
        "profile": copy.deepcopy(profile_overrides or {}),
    }
    return resolved
