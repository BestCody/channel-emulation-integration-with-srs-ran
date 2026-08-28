import json
import math
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class TrajectoryPoint:
    index: int
    time_ns: int
    position: tuple
    velocity: tuple
    speed_mps: float


@dataclass(frozen=True)
class Trajectory:
    name: str
    update_interval_ns: int
    points: tuple

def _vector(value, name):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must contain three values")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain finite values")
    return result


def _norm(value):
    return math.sqrt(sum(item * item for item in value))


def _distance(first, second):
    return _norm(tuple(b - a for a, b in zip(first, second)))


def load_trajectory(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported trajectory schema")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("trajectory name is required")
    interval_ms = float(data.get("update_interval_ms"))
    if not math.isfinite(interval_ms) or interval_ms <= 0.0:
        raise ValueError("update interval must be finite and positive")
    interval_ns = int(round(interval_ms * 1_000_000.0))
    raw_points = data.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ValueError("trajectory requires at least two points")

    points = []
    for expected_index, raw in enumerate(raw_points):
        if not isinstance(raw, dict):
            raise ValueError("trajectory point must be an object")
        index = raw.get("index")
        if index != expected_index:
            raise ValueError("trajectory indexes must be consecutive")
        time_ms = float(raw.get("time_ms"))
        time_ns = int(round(time_ms * 1_000_000.0))
        expected_time = expected_index * interval_ns
        if time_ns != expected_time:
            raise ValueError("trajectory timestamps must match the interval")
        position = _vector(raw.get("position"), "position")
        velocity = _vector(raw.get("velocity"), "velocity")
        speed = float(raw.get("speed_mps"))
        if not math.isfinite(speed) or speed < 0.0:
            raise ValueError("speed must be finite and non-negative")
        if not math.isclose(_norm(velocity), speed, rel_tol=0, abs_tol=1e-9):
            raise ValueError("velocity magnitude does not match speed")
        points.append(
            TrajectoryPoint(index, time_ns, position, velocity, speed)
        )

    if points[0].time_ns != 0:
        raise ValueError("starting position must have timestamp zero")
    for previous, current in zip(points, points[1:]):
        expected_distance = current.speed_mps * interval_ns / 1e9
        actual_distance = _distance(previous.position, current.position)
        if not math.isclose(
            actual_distance,
            expected_distance,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("position step does not match speed and interval")

    return Trajectory(name, interval_ns, tuple(points))


def translate_trajectory(trajectory, offset):
    """Shift all positions by a fixed offset"""
    offset = _vector(list(offset), "offset")
    points = tuple(
        TrajectoryPoint(
            point.index,
            point.time_ns,
            tuple(coordinate + shift for coordinate, shift in zip(point.position, offset)),
            point.velocity,
            point.speed_mps,
        )
        for point in trajectory.points
    )
    return Trajectory(trajectory.name, trajectory.update_interval_ns, points)
