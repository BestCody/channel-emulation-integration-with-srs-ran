import time

from sionna_scene import EXPECTED_SIONNA_RT_VERSION
from sionna_scene import EXPECTED_SIONNA_VERSION
from sionna_scene import EXPECTED_VARIANT
from sionna_scene import _complex_array
from sionna_scene import _solver_options
from sionna_taps import convert_paths


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
        self.sample_rate = float(sample_rate)
        self.config = config
        self.solver_options = _solver_options(config["solver"])
        self.scene = load_scene(scene_path)
        self.scene.frequency = float(carrier_hz)
        antenna = config["antenna"]
        self.scene.tx_array = PlanarArray(
            num_rows=1,
            num_cols=1,
            pattern=antenna["pattern"],
            polarization=antenna["polarization"],
        )
        self.scene.rx_array = PlanarArray(
            num_rows=1,
            num_cols=1,
            pattern=antenna["pattern"],
            polarization=antenna["polarization"],
        )
        if int(self.scene.tx_array.num_ant) != 1:
            raise ValueError("live channel requires one gNB antenna")
        if int(self.scene.rx_array.num_ant) != 1:
            raise ValueError("live channel requires one UE antenna")
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
        if coefficient_array.ndim != 5:
            raise RuntimeError("unexpected CIR coefficient shape")
        if coefficient_array.shape[:4] != (1, 1, 1, 1):
            raise RuntimeError("live channel requires a SISO CIR")
        delay_array = np.asarray(delays)
        if delay_array.ndim != 3 or delay_array.shape[:2] != (1, 1):
            raise RuntimeError("unexpected SISO delay shape")

        conversion_start_ns = time.monotonic_ns()
        coefficient_values = coefficient_array[0, 0, 0, 0].reshape(-1)
        delay_values = delay_array[0, 0].reshape(-1)
        if coefficient_values.size != delay_values.size:
            raise RuntimeError("coefficient and delay shapes differ")
        conversion = convert_paths(
            delay_values.tolist(),
            coefficient_values.tolist(),
            self.sample_rate,
            late_policy=self.config["conversion"]["late_policy"],
            normalization="none",
        )
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
            "conversion": conversion,
        }
