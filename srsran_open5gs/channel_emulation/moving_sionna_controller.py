import argparse
import copy
import json
import math
import pathlib
import sys
import time


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_CONFIG = REPO_ROOT / "configs/ues/srsue-live/config"
sys.path.insert(0, str(LIVE_CONFIG))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from channel_client import ChannelClient
from channel_protocol import Tap as ProtocolTap
from channel_protocol import build_update
from channel_protocol import validate_taps
from sionna_moving import MovingSionnaScene
from sionna_radio_config import load_radio_config
from sionna_scene import load_scene_config
from sionna_scene import sample_ue_positions
from sionna_scene import scene_bounding_box
from sionna_taps import interpolate_taps
from sionna_taps import taps_from_report
from trajectory import load_trajectory
from trajectory import translate_trajectory


def write_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def protocol_taps(conversion):
    if not conversion["safe_to_send"]:
        raise ValueError("Sionna result is not safe to send")
    if conversion["normalization"] != "none":
        raise ValueError("movement test requires absolute coefficients")
    if not conversion["absolute_coefficients_preserved"]:
        raise ValueError("Sionna complex coefficients were not preserved")
    taps = tuple(
        ProtocolTap(tap.delay, tap.coefficient)
        for tap in taps_from_report(conversion)
    )
    return validate_taps(taps)


def noise_sigma(taps, *, configured_sigma=None, snr_db=None):
    if configured_sigma is not None:
        return float(configured_sigma)
    if snr_db is None:
        return 0.0
    channel_power = sum(abs(tap.coefficient) ** 2 for tap in taps)
    return math.sqrt(channel_power / (10.0 ** (float(snr_db) / 10.0)))


def stream_cir(
    client,
    taps,
    sequence,
    ue_index=0,
    configured_sigma=None,
    snr_db=None,
):
    sigma = noise_sigma(
        taps,
        configured_sigma=configured_sigma,
        snr_db=snr_db,
    )
    message = build_update(
        taps=taps,
        sequence=sequence,
        direction="both",
        client_send_ns=time.time_ns(),
        ue_index=ue_index,
        noise_sigma=sigma,
    )
    client.stream(message)
    return sigma


def blend_to_protocol(previous_taps, current_taps, alpha):
    return tuple(
        ProtocolTap(tap.delay, tap.coefficient)
        for tap in interpolate_taps(previous_taps, current_taps, alpha)
    )


def build_ue_setups(args, base_trajectory, num_ues):
    if num_ues == 1 and args.placement_mode != "random":
        config = load_scene_config(args.scene_config)
        config["resolved_placement"] = {
            "mode": "configured",
            "transmitter": config["transmitter"]["position"],
            "receiver": config["receiver"]["position"],
        }
        if tuple(config["receiver"]["position"]) != base_trajectory.points[0].position:
            raise ValueError("scene receiver must match trajectory position 0")
        return [(1, config, base_trajectory)]
    if args.placement_mode != "random":
        raise ValueError(
            "multi-UE (--num-ues > 1) requires --placement-mode random"
        )
    base = load_scene_config(args.scene_config)
    bounds = scene_bounding_box(base["scene"])
    transmitter, receivers = sample_ue_positions(
        bounds,
        num_ues,
        seed=args.placement_seed,
        min_distance=args.placement_min_distance,
    )
    start = tuple(base_trajectory.points[0].position)
    setups = []
    for index, receiver in enumerate(receivers):
        config = copy.deepcopy(base)
        config["transmitter"]["position"] = list(transmitter)
        offset = tuple(
            float(target) - float(point) for target, point in zip(receiver, start)
        )
        trajectory = translate_trajectory(base_trajectory, offset)
        config["receiver"]["position"] = list(trajectory.points[0].position)
        config["resolved_placement"] = {
            "mode": "random",
            "seed": args.placement_seed,
            "transmitter": list(transmitter),
            "receiver": list(trajectory.points[0].position),
            "trajectory_offset": list(offset),
        }
        setups.append((index + 1, config, trajectory))
    return setups


def run_live(args, radio, ue_setups):
    scenes = [
        (
            ue_index,
            MovingSionnaScene(
                config,
                carrier_hz=radio.carrier_hz,
                sample_rate=radio.sample_rate,
            ),
            trajectory,
        )
        for ue_index, config, trajectory in ue_setups
    ]
    client = ChannelClient(
        args.endpoint,
        stream_endpoint=args.stream_endpoint,
        timeout_ms=args.control_timeout_ms,
    )
    records = []
    try:
        config_response = client.get_config()
        if float(config_response["sample_rate"]) != radio.sample_rate:
            raise ValueError("live sample rate does not match")
        if int(config_response["num_ues"]) != len(scenes):
            raise ValueError("live flowgraph UE count does not match")
        steps = args.interp_steps
        update_interval_ns = scenes[0][2].update_interval_ns
        num_points = len(scenes[0][2].points)
        step_sleep_s = update_interval_ns / steps / 1e9
        initial_status = client.get_status()
        sequence = int(initial_status["last_accepted_sequence"]) + 1

        def stream_update(ue_index, taps, index, alpha):
            nonlocal sequence
            sigma = stream_cir(
                client,
                taps,
                sequence,
                ue_index=ue_index,
                configured_sigma=args.noise_sigma,
                snr_db=args.snr_db,
            )
            records.append({
                "ue_index": ue_index,
                "index": index,
                "alpha": alpha,
                "tap_count": len(taps),
                "noise_sigma": sigma,
            })
            sequence += 1

        previous = {}
        for ue_index, scene, trajectory in scenes:
            taps = protocol_taps(
                scene.solve(trajectory.points[0])["conversion"]
            )
            stream_update(ue_index, taps, 0, 1.0)
            previous[ue_index] = taps

        epoch_created_ns = time.monotonic_ns()
        for index in range(1, num_points):
            current = {}
            for ue_index, scene, trajectory in scenes:
                current[ue_index] = protocol_taps(
                    scene.solve(trajectory.points[index])["conversion"]
                )
            for step in range(1, steps + 1):
                alpha = step / steps
                for ue_index, scene, trajectory in scenes:
                    blended = blend_to_protocol(
                        previous[ue_index],
                        current[ue_index],
                        alpha,
                    )
                    stream_update(ue_index, blended, index, alpha)
                time.sleep(step_sleep_s)
            previous = current

        time.sleep(args.final_hold_seconds)
        final_status = client.get_status()
        accepted_delta = (
            int(final_status["accepted_updates"])
            - int(initial_status["accepted_updates"])
        )
        if accepted_delta != len(records):
            raise RuntimeError(
                f"accepted {accepted_delta} of {len(records)} updates"
            )
        if (
            int(final_status["rejected_updates"])
            != int(initial_status["rejected_updates"])
        ):
            raise RuntimeError("the live channel rejected an update")
        if int(final_status["last_accepted_sequence"]) != sequence - 1:
            raise RuntimeError("the final channel sequence is incomplete")
        result = {
            "schema_version": 1,
            "mode": "live-moving-channel-stream",
            "num_ues": len(scenes),
            "update_interval_ns": update_interval_ns,
            "interp_steps": steps,
            "per_symbol_channels": True,
            "noise": {
                "enabled": args.noise_sigma is not None
                or args.snr_db is not None,
                "noise_sigma": args.noise_sigma,
                "snr_db": args.snr_db,
                "snr_definition": (
                    "sum_abs_taps_squared_over_noise_power"
                    if args.snr_db is not None
                    else None
                ),
            },
            "streamed_updates": len(records),
            "epoch_created_monotonic_ns": epoch_created_ns,
            "records": records,
            "final_status": final_status,
        }
        write_json(args.output, result)
        print(json.dumps({
            "output": args.output,
            "streamed_updates": len(records),
            "final_accepted_sequence":
                final_status["last_accepted_sequence"],
        }, sort_keys=True), flush=True)
    finally:
        client.close()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--scene-config", required=True)
    parser.add_argument("--gnb-config", required=True)
    parser.add_argument("--radio-config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--placement-mode",
        choices=["configured", "random"],
        required=True,
    )
    parser.add_argument("--placement-seed", type=int)
    parser.add_argument("--placement-min-distance", type=float)
    parser.add_argument("--num-ues", type=int, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--stream-endpoint", required=True)
    parser.add_argument("--control-timeout-ms", type=int, required=True)
    parser.add_argument("--interp-steps", type=int, required=True)
    parser.add_argument("--final-hold-seconds", type=float, required=True)
    noise = parser.add_mutually_exclusive_group()
    noise.add_argument("--noise-sigma", type=float)
    noise.add_argument("--snr-db", type=float)
    args = parser.parse_args()
    if args.noise_sigma is not None:
        if not math.isfinite(args.noise_sigma) or args.noise_sigma < 0.0:
            parser.error("--noise-sigma must be finite and non-negative")
    if args.snr_db is not None and not math.isfinite(args.snr_db):
        parser.error("--snr-db must be finite")
    if args.interp_steps < 1:
        parser.error("--interp-steps must be at least one")
    if args.control_timeout_ms < 1:
        parser.error("--control-timeout-ms must be at least one")
    if args.placement_mode == "random":
        if args.placement_seed is None:
            parser.error("random placement requires --placement-seed")
        if args.placement_min_distance is None:
            parser.error(
                "random placement requires --placement-min-distance"
            )
    return args


def main():
    args = parse_args()
    radio = load_radio_config(args.gnb_config, args.radio_config)
    trajectory = load_trajectory(args.trajectory)
    num_ues = int(args.num_ues)
    if num_ues < 1:
        raise ValueError("--num-ues must be at least one")
    ue_setups = build_ue_setups(args, trajectory, num_ues)
    run_live(args, radio, ue_setups)


if __name__ == "__main__":
    main()
