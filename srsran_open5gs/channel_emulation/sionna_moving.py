import math
import time

from sionna_scene import EXPECTED_SIONNA_RT_VERSION
from sionna_scene import EXPECTED_SIONNA_VERSION
from sionna_scene import EXPECTED_VARIANT
from sionna_scene import _complex_array
from sionna_scene import _solver_options
from sionna_scene import antenna_array_dims
from sionna_taps import convert_paths
from trajectory import SPEED_OF_LIGHT


def _point_json(point):
    return [
        float(component[0])
        for component in (point.x, point.y, point.z)
    ]


class MovingSionnaScene:
    def __init__(self, config, *, carrier_hz, sample_rate):
        import drjit as dr
        import mitsuba as mi
        import sionna
        import sionna.rt
        from sionna.rt import (
            PathSolver,
            PlanarArray,
            Receiver,
            Transmitter,
            load_scene,
        )
        from sionna.rt import scene as rt_scene

        if sionna.__version__ != EXPECTED_SIONNA_VERSION:
            raise RuntimeError("unexpected Sionna version")
        if sionna.rt.__version__ != EXPECTED_SIONNA_RT_VERSION:
            raise RuntimeError("unexpected Sionna RT version")
        if mi.variant() != EXPECTED_VARIANT:
            raise RuntimeError("unexpected Mitsuba variant")

        scene_path = getattr(rt_scene, config["scene"], None)
        if not isinstance(scene_path, str):
            raise ValueError("unknown bundled scene")
        self.mi = mi
        self.dr = dr
        self.carrier_hz = float(carrier_hz)
        self.sample_rate = float(sample_rate)
        self.config = config
        self.solver_options = _solver_options(config["solver"])
        started = time.monotonic_ns()
        self.scene = load_scene(scene_path)
        self.scene.frequency = self.carrier_hz
        antenna = config["antenna"]
        # One stream per gNB antenna port.
        bs_rows, bs_cols = antenna_array_dims(antenna, "bs_array")
        self.scene.tx_array = PlanarArray(
            num_rows=bs_rows,
            num_cols=bs_cols,
            pattern=antenna["pattern"],
            polarization=antenna["polarization"],
        )
        self.scene.rx_array = PlanarArray(
            num_rows=1,
            num_cols=1,
            pattern=antenna["pattern"],
            polarization=antenna["polarization"],
        )
        self.num_bs_ports = int(self.scene.tx_array.num_ant)
        if int(self.scene.rx_array.num_ant) != 1:
            raise ValueError(
                "live channel supports a single UE antenna port"
            )
        self.transmitter = Transmitter(
            name="gnb",
            position=mi.Point3f(*config["transmitter"]["position"]),
        )
        self.receiver = Receiver(
            name="ue",
            position=mi.Point3f(*config["receiver"]["position"]),
        )
        self.transmitter.look_at(self.receiver)
        self.receiver.look_at(self.transmitter)
        self.scene.add(self.transmitter)
        self.scene.add(self.receiver)
        self.scene.all_set(radio_map=False)
        dr.sync_thread()
        self.solver = PathSolver()
        self.scene_setup_ns = time.monotonic_ns() - started

    def solve(self, trajectory_point):
        import numpy as np

        calculation_start_ns = time.monotonic_ns()
        position_start_ns = calculation_start_ns
        self.receiver.position = self.mi.Point3f(*trajectory_point.position)
        self.transmitter.look_at(self.receiver)
        self.receiver.look_at(self.transmitter)
        self.dr.sync_thread()
        position_end_ns = time.monotonic_ns()

        solve_start_ns = position_end_ns
        paths = self.solver(self.scene, **self.solver_options)
        self.dr.sync_thread()
        solve_end_ns = time.monotonic_ns()

        cir_start_ns = solve_end_ns
        coefficients, delays = paths.cir(
            sampling_frequency=self.sample_rate,
            num_time_steps=1,
            normalize_delays=False,
            out_type="numpy",
        )
        self.dr.sync_thread()
        cir_end_ns = time.monotonic_ns()

        coefficient_array = np.asarray(_complex_array(coefficients))
        if coefficient_array.shape[-1] == 1:
            coefficient_array = coefficient_array[..., 0]
        # Shape: [rx, rx_ant, tx, tx_ant, path].
        if coefficient_array.ndim != 5:
            raise RuntimeError("unexpected CIR coefficient shape")
        if coefficient_array.shape[:3] != (1, 1, 1):
            raise RuntimeError("live channel expects one 1-port UE link")
        if coefficient_array.shape[3] != self.num_bs_ports:
            raise RuntimeError("CIR port count does not match bs_array")
        delay_array = np.asarray(delays)

        conversion_start_ns = time.monotonic_ns()
        conversions = []
        for port in range(self.num_bs_ports):
            coefficient_values = coefficient_array[0, 0, 0, port].reshape(-1)
            if delay_array.ndim == 3:
                # Synthetic ports share delays.
                delay_values = delay_array[0, 0].reshape(-1)
            else:
                delay_values = delay_array[0, 0, 0, port].reshape(-1)
            if coefficient_values.size != delay_values.size:
                raise RuntimeError("coefficient and delay shapes differ")
            conversions.append(convert_paths(
                delay_values.tolist(),
                coefficient_values.tolist(),
                self.sample_rate,
                late_policy=self.config["conversion"]["late_policy"],
                normalization="none",
            ))
        conversion_end_ns = time.monotonic_ns()
        calculation_end_ns = conversion_end_ns
        return {
            "index": trajectory_point.index,
            "trajectory_time_ns": trajectory_point.time_ns,
            "position": list(trajectory_point.position),
            "velocity": list(trajectory_point.velocity),
            "speed_mps": trajectory_point.speed_mps,
            "calculation_start_monotonic_ns": calculation_start_ns,
            "calculation_end_monotonic_ns": calculation_end_ns,
            "timing_ms": {
                "position_update": (position_end_ns-position_start_ns)/1e6,
                "solve": (solve_end_ns-solve_start_ns)/1e6,
                "cir_extraction": (cir_end_ns-cir_start_ns)/1e6,
                "conversion": (conversion_end_ns-conversion_start_ns)/1e6,
                "total": (calculation_end_ns-calculation_start_ns)/1e6,
            },
            "transmitter_position": _point_json(self.transmitter.position),
            "receiver_position": _point_json(self.receiver.position),
            "num_bs_ports": self.num_bs_ports,
            # Port 0 uses the legacy single-port key.
            "conversion": conversions[0],
            "conversions": conversions,
        }

def direct_path_record(point_report):
    valid = [
        path for path in point_report["conversion"]["original_paths"]
        if path["status"] == "valid"
    ]
    if not valid:
        return None
    path = min(valid, key=lambda item: item["delay_seconds"])
    coefficient = complex(
        path["coefficient"]["real"],
        path["coefficient"]["imag"],
    )
    return {
        "path_index": path["index"],
        "delay_seconds": path["delay_seconds"],
        "rounded_sample_delay": round(path["sample_delay"]),
        "coefficient": path["coefficient"],
        "phase_rad": math.atan2(coefficient.imag, coefficient.real),
        "power": path["power"],
    }


def _unwrap(previous, current):
    while current - previous > math.pi:
        current -= 2.0 * math.pi
    while current - previous < -math.pi:
        current += 2.0 * math.pi
    return current


def analyze_phase_progression(point_reports, carrier_hz):
    records = []
    previous_unwrapped = None
    previous_delay = None
    errors = []
    for point in point_reports:
        direct = direct_path_record(point)
        if direct is None:
            errors.append(f"position {point['index']} has no direct path")
            records.append({"index": point["index"], "status": "missing"})
            continue
        wrapped = direct["phase_rad"]
        unwrapped = (
            wrapped if previous_unwrapped is None
            else _unwrap(previous_unwrapped, wrapped)
        )
        expected_delta = None
        actual_delta = None
        error = None
        if previous_unwrapped is not None:
            actual_delta = unwrapped - previous_unwrapped
            expected_delta = -2.0 * math.pi * float(carrier_hz) * (
                direct["delay_seconds"] - previous_delay
            )
            error = actual_delta - expected_delta
            if abs(actual_delta) >= math.pi:
                errors.append(
                    f"position {point['index']} phase jump is too large"
                )
            if abs(error) > 0.25:
                errors.append(
                    f"position {point['index']} phase error {error} exceeds 0.25 rad"
                )
        records.append({
            "index": point["index"],
            "status": "valid",
            **direct,
            "unwrapped_phase_rad": unwrapped,
            "actual_phase_delta_rad": actual_delta,
            "expected_phase_delta_rad": expected_delta,
            "phase_error_rad": error,
            "path_length_m": direct["delay_seconds"] * SPEED_OF_LIGHT,
        })
        previous_unwrapped = unwrapped
        previous_delay = direct["delay_seconds"]
    return {"safe": not errors, "errors": errors, "records": records}


def tap_changes(previous, current):
    def mapping(report):
        return {
            int(tap["delay"]): complex(tap["real"], tap["imag"])
            for tap in report["conversion"]["retained_taps"]
        }

    old = mapping(previous) if previous else {}
    new = mapping(current)
    old_delays = set(old)
    new_delays = set(new)
    return {
        "appeared_delays": sorted(new_delays - old_delays),
        "disappeared_delays": sorted(old_delays - new_delays),
        "changed_delays": sorted(
            delay for delay in old_delays & new_delays
            if old[delay] != new[delay]
        ),
        "previous_tap_count": len(old),
        "current_tap_count": len(new),
        "previous_path_count": (
            0 if previous is None else sum(
                path["status"] == "valid"
                for path in previous["conversion"]["original_paths"]
            )
        ),
        "current_path_count": sum(
            path["status"] == "valid"
            for path in current["conversion"]["original_paths"]
        ),
    }
