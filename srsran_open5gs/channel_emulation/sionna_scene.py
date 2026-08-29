#!/usr/bin/env python3

import copy
import json
import math
import pathlib
import random


EXPECTED_SIONNA_VERSION = "2.0.1"
EXPECTED_SIONNA_RT_VERSION = "2.0.1"
EXPECTED_VARIANT = "cuda_ad_mono_polarized"

PROPAGATION_EFFECTS = (
    "los",
    "specular_reflection",
    "diffuse_reflection",
    "refraction",
    "diffraction",
    "edge_diffraction",
    "diffraction_lit_region",
)
SOLVER_FIELDS = set(PROPAGATION_EFFECTS) | {
    "max_depth",
    "max_num_paths_per_src",
    "samples_per_src",
    "synthetic_array",
    "seed",
}


def _solver_options(solver):
    return dict(solver)


def _validate_scene_config(config):
    required = {
        "scene",
        "transmitter",
        "receiver",
        "antenna",
        "solver",
        "conversion",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"scene config is missing {sorted(missing)}")
    unknown = set(config) - required
    if unknown:
        raise ValueError(f"scene config has unknown fields: {sorted(unknown)}")
    for endpoint in ("transmitter", "receiver"):
        value = config[endpoint]
        if not isinstance(value, dict) or set(value) != {"position"}:
            raise ValueError(f"{endpoint} must contain only position")
        position = value["position"]
        if (
            not isinstance(position, list)
            or len(position) != 3
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in position
            )
        ):
            raise ValueError(f"{endpoint} position must be finite 3D")
    antenna = config["antenna"]
    if not isinstance(antenna, dict):
        raise ValueError("antenna must be an object")
    unknown = set(antenna) - {"pattern", "polarization"}
    if unknown:
        raise ValueError(f"unsupported antenna fields: {sorted(unknown)}")
    if antenna.get("polarization") not in {"V", "H"}:
        raise ValueError("SISO polarization must be V or H")
    if config["solver"].get("synthetic_array") is not True:
        raise ValueError("the live SISO channel requires synthetic_array")
    if set(config["solver"]) != SOLVER_FIELDS:
        raise ValueError(
            f"solver must contain exactly {sorted(SOLVER_FIELDS)}"
        )
    if set(config["conversion"]) != {"late_policy"}:
        raise ValueError("conversion must contain only late_policy")
    if config["conversion"]["late_policy"] != "reject":
        raise ValueError("conversion late_policy must be reject")
def _distance(first, second):
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(first, second)))


def scene_bounding_box(scene_name):
    from sionna.rt import load_scene
    from sionna.rt import scene as rt_scene

    scene_path = getattr(rt_scene, scene_name, None)
    if not isinstance(scene_path, str):
        raise ValueError(f"unknown bundled scene: {scene_name}")
    scene = load_scene(scene_path)
    bbox = scene.mi_scene.bbox()
    scene_min = bbox.min
    scene_max = bbox.max
    lower = [float(scene_min.x), float(scene_min.y), float(scene_min.z)]
    upper = [float(scene_max.x), float(scene_max.y), float(scene_max.z)]
    if any(not math.isfinite(value) for value in lower + upper):
        raise ValueError(f"scene {scene_name} has a non-finite bounding box")
    return lower, upper


def _random_point(lower, upper, rng):
    return [rng.uniform(lo, hi) for lo, hi in zip(lower, upper)]


def sample_ue_positions(bounds, num_ues, *, seed, min_distance):
    num_ues = int(num_ues)
    if num_ues < 1:
        raise ValueError("num_ues must be at least one")
    lower, upper = bounds
    min_distance = float(min_distance)
    if min_distance < 0.0 or not math.isfinite(min_distance):
        raise ValueError("placement min_distance_m must be finite and non-negative")
    if min_distance > _distance(lower, upper):
        raise ValueError(
            "placement min_distance_m exceeds the scene bounding box; "
            "transmitter and receiver cannot be separated that far"
        )
    rng = random.Random(seed)
    transmitter = _random_point(lower, upper, rng)
    receivers = []
    for _ in range(num_ues):
        receiver = _random_point(lower, upper, rng)
        while _distance(transmitter, receiver) < min_distance:
            receiver = _random_point(lower, upper, rng)
        receivers.append(receiver)
    return transmitter, receivers


def load_scene_config(path):
    config = json.loads(
        pathlib.Path(path).read_text(encoding="utf-8")
    )
    _validate_scene_config(config)
    return copy.deepcopy(config)


def _complex_array(value):
    import numpy as np

    if isinstance(value, tuple) and len(value) == 2:
        return np.asarray(value[0]) + 1j * np.asarray(value[1])
    return np.asarray(value)
