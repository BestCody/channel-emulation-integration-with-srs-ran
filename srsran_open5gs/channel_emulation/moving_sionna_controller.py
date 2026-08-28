import argparse
import copy
import os
import json
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
from sionna_scene import antenna_array_dims
from sionna_scene import antenna_port_count
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


def protocol_taps_per_port(point_report):
    return tuple(
        protocol_taps(conversion)
        for conversion in point_report["conversions"]
    )


def stream_cir(client, taps, sequence, ue_index=0, bs_index=0):
    message = build_update(
        taps=taps,
        sequence=sequence,
        direction="both",
        client_send_ns=time.time_ns(),
        ue_index=ue_index,
        bs_index=bs_index,
    )
    client.stream(message)


def scene_bs_ports(config):
    antenna = config["antenna"]
    rows, cols = antenna_array_dims(antenna, "bs_array")
    return antenna_port_count(rows, cols, antenna["polarization"])


def blend_to_protocol(previous_taps, current_taps, alpha):
    return tuple(
        ProtocolTap(tap.delay, tap.coefficient)
        for tap in interpolate_taps(previous_taps, current_taps, alpha)
    )


def build_ue_setups(args, base_trajectory, num_ues):
    if num_ues == 1 and args.placement_mode != "random":
        config = load_scene_config(
            args.scene_config,
            placement_mode=args.placement_mode,
            placement_seed=args.placement_seed,
            min_distance_m=args.placement_min_distance,
        )
        if tuple(config["receiver"]["position"]) != base_trajectory.points[0].position:
            raise ValueError("scene receiver must match trajectory position 0")
        return [(1, config, base_trajectory)]
    if args.placement_mode != "random":
        raise ValueError(
            "multi-UE (--num-ues > 1) requires --placement-mode random"
        )
    base = load_scene_config(args.scene_config, placement_mode="configured")
    bounds = scene_bounding_box(base["scene"])
    min_distance = (
        args.placement_min_distance
        if args.placement_min_distance is not None
        else base.get("placement", {}).get("min_distance_m", 0.0)
    )
    transmitter, receivers = sample_ue_positions(
        bounds,
        num_ues,
        seed=args.placement_seed,
        min_distance=min_distance,
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
    num_bs_ports = scene_bs_ports(ue_setups[0][1])
    client = ChannelClient(args.endpoint, stream_endpoint=args.stream_endpoint)
    records = []
    try:
        config_response = client.get_config()
        if float(config_response["sample_rate"]) != radio.sample_rate:
            raise ValueError("live sample rate does not match")
        if int(config_response.get("num_ues", 1)) != len(scenes):
            raise ValueError("live flowgraph UE count does not match")
        if int(config_response.get("gnb_antennas", 1)) != num_bs_ports:
            raise ValueError(
                "live flowgraph gNB antenna count does not match "
                "the scene bs_array"
            )

        steps = max(1, int(args.interp_steps))
        update_interval_ns = scenes[0][2].update_interval_ns
        num_points = len(scenes[0][2].points)
        step_sleep_s = update_interval_ns / steps / 1e9
        sequence = int(client.get_status()["last_accepted_sequence"]) + 1

        def stream_ports(ue_index, port_taps, index, alpha):
            nonlocal sequence
            for port, taps in enumerate(port_taps):
                stream_cir(
                    client, taps, sequence,
                    ue_index=ue_index, bs_index=port + 1,
                )
                records.append({
                    "ue_index": ue_index,
                    "bs_index": port + 1,
                    "index": index,
                    "alpha": alpha,
                    "tap_count": len(taps),
                })
                sequence += 1

        previous = {}
        for ue_index, scene, trajectory in scenes:
            port_taps = protocol_taps_per_port(
                scene.solve(trajectory.points[0])
            )
            stream_ports(ue_index, port_taps, 0, 1.0)
            previous[ue_index] = port_taps

        epoch_created_ns = time.monotonic_ns()
        for index in range(1, num_points):
            current = {}
            for ue_index, scene, trajectory in scenes:
                current[ue_index] = protocol_taps_per_port(
                    scene.solve(trajectory.points[index])
                )
            for step in range(1, steps + 1):
                alpha = step / steps
                for ue_index, scene, trajectory in scenes:
                    blended = tuple(
                        blend_to_protocol(before, after, alpha)
                        for before, after in zip(
                            previous[ue_index], current[ue_index]
                        )
                    )
                    stream_ports(ue_index, blended, index, alpha)
                time.sleep(step_sleep_s)
            previous = current

        time.sleep(args.final_hold_seconds)
        final_status = client.get_status()
        result = {
            "schema_version": 1,
            "mode": "live-moving-channel-stream",
            "num_ues": len(scenes),
            "gnb_antennas": num_bs_ports,
            "update_interval_ns": update_interval_ns,
            "interp_steps": steps,
            "per_symbol_channels": True,
            "noise_enabled": False,
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
    parser.add_argument(
        "--trajectory",
        default=str(REPO_ROOT / "channel_emulation/trajectories/default_trajectory.json"),
    )
    parser.add_argument(
        "--scene-config",
        default=str(REPO_ROOT / "channel_emulation/scenes/default_scene.json"),
    )
    parser.add_argument(
        "--gnb-config",
        default=str(REPO_ROOT / "configs/srsRAN/srsran-gnb/config/srsran-gnb.yaml"),
    )
    parser.add_argument(
        "--ue-config",
        default=str(REPO_ROOT / "configs/ues/srsue/config/ue0.conf"),
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--placement-mode", choices=["configured", "random"])
    parser.add_argument("--placement-seed", type=int)
    parser.add_argument("--placement-min-distance", type=float)
    parser.add_argument("--num-ues", type=int, default=1)
    parser.add_argument("--endpoint", default=os.environ.get("CHANNEL_CONTROL_ENDPOINT", "tcp://127.0.0.1:5555"))
    parser.add_argument("--stream-endpoint", default=os.environ.get("CHANNEL_STREAM_ENDPOINT", "tcp://127.0.0.1:5556"))
    parser.add_argument("--interp-steps", type=int, default=8)
    parser.add_argument("--final-hold-seconds", type=float, default=5.0)
    return parser.parse_args()


def main():
    args = parse_args()
    radio = load_radio_config(args.gnb_config, args.ue_config)
    trajectory = load_trajectory(args.trajectory)
    num_ues = int(args.num_ues)
    if num_ues < 1:
        raise ValueError("--num-ues must be at least one")
    ue_setups = build_ue_setups(args, trajectory, num_ues)
    run_live(args, radio, ue_setups)


if __name__ == "__main__":
    main()
