#!/usr/bin/env python3

import argparse
import configparser
import math


def validate_sample_rate(sample_rate):
    sample_rate = float(sample_rate)
    if not math.isfinite(sample_rate):
        raise ValueError("sample rate must be finite")
    if sample_rate <= 0:
        raise ValueError("sample rate must be greater than zero")
    return sample_rate


def samples_per_symbol(sample_rate, scs_khz=15.0):
    # Mean OFDM symbol length.
    sample_rate = validate_sample_rate(sample_rate)
    scs_khz = float(scs_khz)
    if not math.isfinite(scs_khz) or scs_khz < 15.0:
        raise ValueError("scs_khz must be a numerology >= 15")
    mu = round(math.log2(scs_khz / 15.0))
    symbols_per_second = 14000.0 * (2 ** mu)
    return max(1, int(round(sample_rate / symbols_per_second)))


def sample_rate_from_ue_config(path):
    parser = configparser.ConfigParser()
    loaded = parser.read(path, encoding="utf-8")
    if not loaded:
        raise ValueError(f"could not read UE configuration: {path}")

    try:
        configured_rate = parser["rf"]["srate"]
    except KeyError as error:
        raise ValueError(
            f"UE configuration has no [rf] srate value: {path}"
        ) from error

    return validate_sample_rate(configured_rate)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fixed-channel configuration helpers"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sample_rate_parser = subparsers.add_parser(
        "sample-rate",
        help="print the [rf] sample rate from an srsUE configuration",
    )
    sample_rate_parser.add_argument("config")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "sample-rate":
        print(format(sample_rate_from_ue_config(args.config), ".12g"))


if __name__ == "__main__":
    main()
