import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHANNEL_DIR = ROOT / "channel_emulation"
LIVE_CONFIG = ROOT / "configs/ues/srsue-live/config"
sys.path.insert(0, str(CHANNEL_DIR))
sys.path.insert(0, str(LIVE_CONFIG))

from channel_protocol import Tap as ProtocolTap
from channel_protocol import build_update
from channel_protocol import decode_message
from channel_protocol import encode_message
from channel_protocol import parse_update
from sionna_taps import Tap
from sionna_taps import convert_paths
from sionna_taps import interpolate_taps
from trajectory import load_trajectory


class ChannelProtocolTests(unittest.TestCase):
    def test_binary_update_round_trip(self):
        message = build_update(
            (ProtocolTap(0, 1 + 0j), ProtocolTap(3, 0.25 - 0.5j)),
            sequence=17,
            client_send_ns=123,
            ue_index=1,
        )
        decoded = decode_message(encode_message(message))
        update = parse_update(decoded)
        self.assertEqual(update.sequence, 17)
        self.assertEqual(update.ue_index, 1)
        self.assertEqual(update.taps[1].delay, 3)
        self.assertEqual(update.taps[1].coefficient, 0.25 - 0.5j)

    def test_duplicate_delays_are_combined(self):
        message = build_update(
            (ProtocolTap(2, 1 + 0j), ProtocolTap(2, -0.25 + 0j)),
            sequence=1,
        )
        update = parse_update(message)
        self.assertEqual(update.taps, (ProtocolTap(2, 0.75 + 0j),))


class ChannelConversionTests(unittest.TestCase):
    def test_absolute_path_power_is_preserved(self):
        report = convert_paths(
            [0.0],
            [0.5 + 0.5j],
            sample_rate=1_000_000,
        )
        self.assertTrue(report["safe_to_send"])
        self.assertTrue(report["absolute_coefficients_preserved"])
        self.assertAlmostEqual(report["retained_power"], 0.5)

    def test_late_path_is_rejected(self):
        report = convert_paths(
            [0.01],
            [1 + 0j],
            sample_rate=1_000_000,
            max_channel_len=32,
        )
        self.assertFalse(report["safe_to_send"])
        self.assertTrue(report["errors"])

    def test_interpolation_uses_both_endpoints(self):
        start = (Tap(0, 1 + 0j),)
        end = (Tap(0, 0 + 0j), Tap(2, 1 + 0j))
        middle = interpolate_taps(start, end, 0.5)
        self.assertEqual(middle, (Tap(0, 0.5 + 0j), Tap(2, 0.5 + 0j)))


class TrajectoryTests(unittest.TestCase):
    def test_default_trajectory_loads(self):
        trajectory = load_trajectory(
            ROOT / "channel_emulation/trajectories/default_trajectory.json"
        )
        self.assertGreater(len(trajectory.points), 1)


if __name__ == "__main__":
    unittest.main()
