import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiment_framework.config import load_and_resolve_study
from experiment_framework.config import ConfigError
from experiment_framework.lifecycle import CommandExecutor
from experiment_framework.lifecycle import CommandFailure
from experiment_framework.lifecycle import command_environment
from experiment_framework.lifecycle import kubernetes_image_names


class StudyTests(unittest.TestCase):
    def resolve(self, name):
        return load_and_resolve_study(
            ROOT / f"experiments/studies/{name}.json",
            resolved_at="2026-01-01T00:00:00+00:00",
            condition_overrides={
                "propagation": {
                    "los": True,
                    "specular_reflection": True,
                }
            },
        )

    def test_live_siso_resolves(self):
        study = self.resolve("live-siso")
        self.assertEqual(study["trial_count"], 1)
        self.assertEqual(study["conditions"][0]["condition_id"], "live-siso")
        self.assertEqual(
            study["parameters"]["runtime_images"]
            ["required_in_kubernetes"],
            ["localhost/srsue-live:gr38-v1"],
        )

    def test_network_catalog_resolves_repetitions(self):
        study = self.resolve("network-catalog")
        self.assertEqual(study["trial_count"], 25)
        self.assertEqual(len(study["conditions"]), 5)

    def test_snr_sweep_resolves_noise_levels(self):
        study = self.resolve("snr-sweep")
        self.assertEqual(study["trial_count"], 25)
        self.assertEqual(
            [item["noise"]["snr_db"] for item in study["conditions"]],
            [35.0, 30.0, 25.0, 20.0, 15.0],
        )

    def test_multi_ue_study_resolves_two_ues(self):
        study = self.resolve("multi-ue-validation")
        self.assertEqual(study["trial_count"], 1)
        self.assertEqual(study["parameters"]["radio"]["ue_number"], 2)

    def test_noise_controls_are_mutually_exclusive(self):
        with self.assertRaises(ConfigError):
            load_and_resolve_study(
                ROOT / "experiments/studies/live-siso.json",
                condition_overrides={
                    "noise": {"sigma": 0.1, "snr_db": 20.0}
                },
            )

    def test_unknown_condition_field_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_and_resolve_study(
                ROOT / "experiments/studies/live-siso.json",
                condition_overrides={"legacy_mode": True},
            )

    def test_unknown_benchmark_field_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_and_resolve_study(
                ROOT / "experiments/studies/live-siso.json",
                parameter_overrides={"fallback_mode": True},
            )

    def test_command_environment_is_copied(self):
        original = {"PATH": "/bin"}
        cleaned = command_environment(original)
        self.assertEqual(cleaned, original)
        self.assertIsNot(cleaned, original)

    def test_capture_does_not_mix_stderr_into_stdout(self):
        executor = CommandExecutor(cwd=ROOT)
        output = executor.capture([
            sys.executable,
            "-c",
            "import sys; print('value'); print('diagnostic', file=sys.stderr)",
        ])
        self.assertEqual(output, "value")

    def test_capture_reports_stderr_on_failure(self):
        executor = CommandExecutor(cwd=ROOT)
        with self.assertRaisesRegex(CommandFailure, "failure detail"):
            executor.capture([
                sys.executable,
                "-c",
                "import sys; print('failure detail', file=sys.stderr); sys.exit(2)",
            ])

    def test_image_inventory_accepts_unnamed_images(self):
        nodes = {
            "items": [
                {
                    "status": {
                        "images": [
                            {"names": None},
                            {"names": ["localhost/image:tag"]},
                        ]
                    }
                }
            ]
        }
        self.assertEqual(
            kubernetes_image_names(nodes),
            {"localhost/image:tag"},
        )


if __name__ == "__main__":
    unittest.main()
