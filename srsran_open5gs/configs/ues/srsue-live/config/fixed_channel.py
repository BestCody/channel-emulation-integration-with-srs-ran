#!/usr/bin/env python3

import argparse
import json
import math
import pathlib


def validate_sample_rate(sample_rate):
    sample_rate = float(sample_rate)
    if not math.isfinite(sample_rate):
        raise ValueError("sample rate must be finite")
    if sample_rate <= 0:
        raise ValueError("sample rate must be greater than zero")
    return sample_rate


def samples_per_symbol(sample_rate):
    sample_rate = validate_sample_rate(sample_rate)
    result = int(round(sample_rate / 14000.0))
    if result < 1:
        raise ValueError("sample rate is below one sample per symbol")
    return result


def sample_rate_from_radio_config(path):
    try:
        config = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        configured_rate = config["sample_rate"]
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid radio configuration: {path}") from error
    return validate_sample_rate(configured_rate)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fixed-channel configuration helpers"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sample_rate_parser = subparsers.add_parser(
        "sample-rate",
        help="print the configured sample rate",
    )
    sample_rate_parser.add_argument("config")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "sample-rate":
        print(format(sample_rate_from_radio_config(args.config), ".12g"))


if __name__ == "__main__":
    main()
