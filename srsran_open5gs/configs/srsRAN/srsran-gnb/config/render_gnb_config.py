#!/usr/bin/env python3

import os
import pathlib
import re
import sys


PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _required_env(name):
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _device_args(downlink, uplink):
    return (
        f"tx_port={downlink},rx_port={uplink},"
        "base_srate=23.04e6"
    )


def render_values():
    gnb_bind_addr = _required_env("SRSRAN_GNB_N3_BIND_ADDR")
    gnb_zmq_addr = _required_env("SRSRAN_GNB_ZMQ_ADDR")
    ue_zmq_addr = _required_env("SRSRAN_UE_ZMQ_ADDR")
    downlink = f"tcp://{gnb_zmq_addr}:2000"
    uplink = f"tcp://{ue_zmq_addr}:2001"
    return {
        "SRSRAN_AMF_N3_ADDR": _required_env("SRSRAN_AMF_N3_ADDR"),
        "SRSRAN_GNB_N3_BIND_ADDR": gnb_bind_addr,
        "SRSRAN_ZMQ_GNB_DEVICE_ARGS": _device_args(downlink, uplink),
    }


def render_text(text):
    values = render_values()

    def replace(match):
        key = match.group(1)
        if key in values:
            return values[key]
        raise KeyError(f"no value configured for {key}")

    return PLACEHOLDER.sub(replace, text)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(
            "Usage: render_gnb_config.py TEMPLATE_PATH OUTPUT_PATH"
        )
    template = pathlib.Path(sys.argv[1])
    output = pathlib.Path(sys.argv[2])
    output.write_text(
        render_text(template.read_text(encoding="utf-8")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
