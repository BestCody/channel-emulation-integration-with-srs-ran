#!/usr/bin/env python3

import math
from dataclasses import dataclass


DEFAULT_CHANNEL_LEN = 1024
DEFAULT_SINC_HALF_WIDTH = 8
KERNEL_EPS = 1e-9


@dataclass(frozen=True)
class Tap:
    delay: int
    coefficient: complex


def _complex_json(value):
    return {"real": float(value.real), "imag": float(value.imag)}


def _tap_json(tap):
    return {
        "delay": int(tap.delay),
        **_complex_json(tap.coefficient),
        "power": float(abs(tap.coefficient) ** 2),
    }


def _round_sample_delay(delay_seconds, sample_rate):
    return int(math.floor(delay_seconds * sample_rate + 0.5))


def _sinc(x):
    if x == 0.0:
        return 1.0
    px = math.pi * x
    return math.sin(px) / px


def _blackman(x, half):
    if abs(x) > half:
        return 0.0
    return (
        0.42
        + 0.5 * math.cos(math.pi * x / half)
        + 0.08 * math.cos(2.0 * math.pi * x / half)
    )


def _fractional_delay_kernel(frac_delay, half_width):
    base = math.floor(frac_delay)
    kernel = {}
    for index in range(base - half_width + 1, base + half_width + 1):
        offset = index - frac_delay
        weight = _sinc(offset) * _blackman(offset, half_width)
        if abs(weight) > KERNEL_EPS:
            kernel[index] = weight
    return kernel


def convert_paths(
    delays,
    coefficients,
    sample_rate,
    *,
    max_channel_len=DEFAULT_CHANNEL_LEN,
    sinc_half_width=DEFAULT_SINC_HALF_WIDTH,
    late_policy="reject",
    normalization="none",
):
    if late_policy not in {"reject", "drop"}:
        raise ValueError("late_policy must be reject or drop")
    if normalization not in {"none", "unit_energy"}:
        raise ValueError("normalization must be none or unit_energy")
    sample_rate = float(sample_rate)
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be finite and positive")
    max_channel_len = int(max_channel_len)
    if max_channel_len < 1:
        raise ValueError("max_channel_len must be a positive integer")
    sinc_half_width = int(sinc_half_width)
    if sinc_half_width < 1:
        raise ValueError("sinc_half_width must be a positive integer")
    if len(delays) != len(coefficients):
        raise ValueError("delay and coefficient counts must match")

    original_paths = []
    combined = {}
    errors = []
    late_path_power = 0.0
    truncated_edge_power = 0.0

    for index, (delay_value, coefficient_value) in enumerate(
        zip(delays, coefficients)
    ):
        delay_seconds = float(delay_value)
        coefficient = complex(coefficient_value)
        power = float(abs(coefficient) ** 2)
        record = {
            "index": index,
            "delay_seconds": delay_seconds,
            "sample_delay": None,
            "coefficient": _complex_json(coefficient),
            "power": power,
            "status": "valid",
        }

        if delay_seconds == -1.0 and coefficient == 0.0j:
            record["status"] = "sionna_padding"
            original_paths.append(record)
            continue
        if (
            not math.isfinite(delay_seconds)
            or not math.isfinite(coefficient.real)
            or not math.isfinite(coefficient.imag)
        ):
            record["status"] = "invalid"
            errors.append(f"path {index} contains a non-finite value")
            original_paths.append(record)
            continue
        if delay_seconds < 0:
            record["status"] = "invalid"
            errors.append(f"path {index} has a negative delay")
            original_paths.append(record)
            continue

        frac_delay = delay_seconds * sample_rate
        record["sample_delay"] = frac_delay
        if _round_sample_delay(delay_seconds, sample_rate) > max_channel_len - 1:
            record["status"] = "late"
            late_path_power += power
            if late_policy == "reject":
                errors.append(
                    f"path {index} at sample delay {frac_delay:.3f} exceeds "
                    f"channel length {max_channel_len}"
                )
            original_paths.append(record)
            continue

        kernel = _fractional_delay_kernel(frac_delay, sinc_half_width)
        norm = math.sqrt(sum(weight * weight for weight in kernel.values()))
        if norm <= 0.0:
            record["status"] = "invalid"
            errors.append(f"path {index} produced an empty interpolation kernel")
            original_paths.append(record)
            continue
        for sample_index, weight in kernel.items():
            contribution = coefficient * (weight / norm)
            if sample_index < 0 or sample_index > max_channel_len - 1:
                truncated_edge_power += float(abs(contribution) ** 2)
                continue
            combined[sample_index] = (
                combined.get(sample_index, 0.0j) + contribution
            )
        original_paths.append(record)

    combined_taps = tuple(
        Tap(delay, coefficient)
        for delay, coefficient in sorted(combined.items())
        if coefficient != 0.0j
    )
    combined_power = float(
        sum(abs(tap.coefficient) ** 2 for tap in combined_taps)
    )

    retained = combined_taps
    retained_power_before_normalization = combined_power
    if normalization == "unit_energy" and retained:
        if retained_power_before_normalization <= 0:
            errors.append("channel energy is zero")
        else:
            scale = 1.0 / math.sqrt(retained_power_before_normalization)
            retained = tuple(
                Tap(tap.delay, tap.coefficient * scale) for tap in retained
            )

    if not combined_taps:
        errors.append("dense channel is empty")

    retained_power = float(
        sum(abs(tap.coefficient) ** 2 for tap in retained)
    )
    original_power = float(
        sum(
            path["power"]
            for path in original_paths
            if path["status"] in {"valid", "late"}
        )
    )
    channel_length = (combined_taps[-1].delay + 1) if combined_taps else 0

    return {
        "safe_to_send": not errors,
        "errors": errors,
        "sample_rate": sample_rate,
        "max_channel_len": max_channel_len,
        "sinc_half_width": sinc_half_width,
        "channel_length": channel_length,
        "late_policy": late_policy,
        "normalization": normalization,
        "absolute_coefficients_preserved": normalization == "none",
        "original_paths": original_paths,
        "original_path_power": original_power,
        "combined_taps": [_tap_json(tap) for tap in combined_taps],
        "combined_power": combined_power,
        "retained_taps": [_tap_json(tap) for tap in retained],
        "retained_power_before_normalization":
            retained_power_before_normalization,
        "retained_power": retained_power,
        "discarded_power": late_path_power + truncated_edge_power,
        "discarded_late_path_power": late_path_power,
        "discarded_edge_power": truncated_edge_power,
    }


def taps_from_report(report):
    return tuple(
        Tap(
            int(tap["delay"]),
            complex(float(tap["real"]), float(tap["imag"])),
        )
        for tap in report["retained_taps"]
    )


def interpolate_taps(taps_a, taps_b, alpha):
    if not 0.0 <= float(alpha) <= 1.0:
        raise ValueError("alpha must be in [0, 1]")
    alpha = float(alpha)
    combined = {}
    for tap in taps_a:
        delay = int(tap.delay)
        combined[delay] = combined.get(delay, 0.0j) + (
            1.0 - alpha
        ) * complex(tap.coefficient)
    for tap in taps_b:
        delay = int(tap.delay)
        combined[delay] = combined.get(delay, 0.0j) + alpha * complex(
            tap.coefficient
        )
    return tuple(
        Tap(delay, coefficient)
        for delay, coefficient in sorted(combined.items())
        if coefficient != 0.0j
    )
