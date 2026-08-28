#!/usr/bin/env python3

import json
import math
import pathlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RadioConfig:
    nr_arfcn: int
    band: int
    carrier_hz: float
    sample_rate: float


def nr_arfcn_to_hz(nr_arfcn):
    if isinstance(nr_arfcn, bool) or not isinstance(nr_arfcn, int):
        raise ValueError("NR-ARFCN must be an integer")
    if 0 <= nr_arfcn <= 599999:
        return nr_arfcn * 5_000.0
    if 600000 <= nr_arfcn <= 2016666:
        return 3_000_000_000.0 + (nr_arfcn - 600000) * 15_000.0
    if 2016667 <= nr_arfcn <= 3279165:
        return (
            24_250_080_000.0
            + (nr_arfcn - 2016667) * 60_000.0
        )
    raise ValueError("NR-ARFCN is outside the global raster")


def _yaml_integer(text, key):
    match = re.search(
        rf"(?m)^\s*{re.escape(key)}\s*:\s*([0-9]+)\b",
        text,
    )
    if match is None:
        raise ValueError(f"{key} was not found")
    return int(match.group(1))


def gnb_radio_config(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    nr_arfcn = _yaml_integer(text, "dl_arfcn")
    band = _yaml_integer(text, "band")
    return nr_arfcn, band, nr_arfcn_to_hz(nr_arfcn)


def radio_sample_rate(path):
    try:
        config = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        sample_rate = float(config["sample_rate"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("radio sample rate was not found") from error
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("UE RF sample rate must be finite and positive")
    return sample_rate


def load_radio_config(gnb_path, radio_path):
    nr_arfcn, band, carrier_hz = gnb_radio_config(gnb_path)
    return RadioConfig(
        nr_arfcn=nr_arfcn,
        band=band,
        carrier_hz=carrier_hz,
        sample_rate=radio_sample_rate(radio_path),
    )
