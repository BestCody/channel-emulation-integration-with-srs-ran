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


def _solver_options(solver):
    """Resolve toggles; default all effects off."""
    options = dict(solver)
    for effect in PROPAGATION_EFFECTS:
        options.setdefault(effect, False)
    return options


def _validate_scene_config(config):
    required = {
        "scene",
        "transmitter",
        "receiver",
        "placement",
        "antenna",
        "solver",
        "conversion",
    }
    missing = required - set(config)
    if missing:
        raise ValueError(f"scene config is missing {sorted(missing)}")
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
    placement = config["placement"]
    if not isinstance(placement, dict):
        raise ValueError("placement must be an object")
    if placement.get("mode") not in {"configured", "random"}:
        raise ValueError("placement mode must be configured or random")
    if "min_distance_m" not in placement:
        raise ValueError("placement min_distance_m is required")


def _distance(first, second):
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(first, second)))


def scene_bounding_box(scene_name):
    """Return the physical scene bounds."""
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


def _apply_random_placement(
    config,
    bounds,
    *,
    placement_seed=None,
    min_distance_m=None,
):
    placement = config["placement"]
    seed = placement_seed if placement_seed is not None else placement.get("seed")
    lower, upper = bounds
    min_distance = float(
        min_distance_m
        if min_distance_m is not None
        else placement["min_distance_m"]
    )
    original = {
        "transmitter": copy.deepcopy(config["transmitter"].get("position")),
        "receiver": copy.deepcopy(config["receiver"].get("position")),
    }
    transmitter, receivers = sample_ue_positions(
        bounds, 1, seed=seed, min_distance=min_distance
    )
    receiver = receivers[0]
    config["transmitter"]["position"] = transmitter
    config["receiver"]["position"] = receiver
    config["resolved_placement"] = {
        "mode": "random",
        "seed": seed,
        "scene_bounds": {"min": lower, "max": upper},
        "original": original,
        "transmitter": transmitter,
        "receiver": receiver,
        "min_distance_m": min_distance,
    }
    return config


def sample_ue_positions(bounds, num_ues, *, seed=None, min_distance=0.0):
    """Sample one TX and several valid UE positions."""
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


def load_scene_config(
    path,
    *,
    placement_mode=None,
    placement_seed=None,
    min_distance_m=None,
    scene_bounds=None,
):
    config = json.loads(
        pathlib.Path(path).read_text(encoding="utf-8")
    )
    _validate_scene_config(config)
    config = copy.deepcopy(config)
    placement = config["placement"]
    mode = placement["mode"] if placement_mode is None else placement_mode
    if mode == "configured":
        config["resolved_placement"] = {
            "mode": "configured",
            "transmitter": config["transmitter"].get("position"),
            "receiver": config["receiver"].get("position"),
        }
        return config
    if mode == "random":
        bounds = scene_bounds or scene_bounding_box(config["scene"])
        return _apply_random_placement(
            config,
            bounds,
            placement_seed=placement_seed,
            min_distance_m=min_distance_m,
        )
    raise ValueError(f"unsupported placement mode: {mode}")


def _complex_array(value):
    import numpy as np

    if isinstance(value, tuple) and len(value) == 2:
        return np.asarray(value[0]) + 1j * np.asarray(value[1])
    return np.asarray(value)
