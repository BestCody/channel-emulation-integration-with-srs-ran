#!/usr/bin/env python3

import os
import subprocess


DEFAULT_AMF_N3_ADDR = "10.10.3.200"
DEFAULT_GNB_ZMQ_ADDR = "10.10.3.231"
DEFAULT_UE_ZMQ_ADDR = "10.10.3.232"
DEFAULT_ZMQ_INTERFACE = "n3"


def _env(name):
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(name, default):
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _interface_ipv4(name):
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", name],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        if "inet" in parts:
            cidr = parts[parts.index("inet") + 1]
            return cidr.split("/", 1)[0]
    return None


def _endpoint(env_name, addr, port):
    return _env(env_name) or f"tcp://{addr}:{port}"


def amf_n3_addr():
    return _env("SRSRAN_AMF_N3_ADDR") or DEFAULT_AMF_N3_ADDR


def gnb_zmq_addr():
    return _env("SRSRAN_GNB_ZMQ_ADDR") or DEFAULT_GNB_ZMQ_ADDR


def ue_zmq_addr():
    interface = _env("SRSRAN_ZMQ_INTERFACE") or DEFAULT_ZMQ_INTERFACE
    return (
        _env("SRSRAN_UE_ZMQ_ADDR")
        or _interface_ipv4(interface)
        or DEFAULT_UE_ZMQ_ADDR
    )


def gnb_antennas():
    return _env_int("SRSRAN_GNB_ANTENNAS", 1)


def gnb_downlink_endpoint(antenna=0):
    # Port 0 uses the legacy endpoint pair.
    if antenna == 0:
        return _endpoint(
            "SRSRAN_ZMQ_GNB_DOWNLINK_ENDPOINT",
            gnb_zmq_addr(),
            _env_int("SRSRAN_ZMQ_GNB_DOWNLINK_PORT", 2000),
        )
    base = _env_int("SRSRAN_ZMQ_GNB_DOWNLINK_PORT", 2000)
    return f"tcp://{gnb_zmq_addr()}:{base + 2 * antenna}"


def gnb_uplink_endpoint(antenna=0):
    if antenna == 0:
        return _endpoint(
            "SRSRAN_ZMQ_GNB_UPLINK_ENDPOINT",
            ue_zmq_addr(),
            _env_int("SRSRAN_ZMQ_GNB_UPLINK_PORT", 2001),
        )
    base = _env_int("SRSRAN_ZMQ_GNB_UPLINK_PORT", 2001)
    return f"tcp://{ue_zmq_addr()}:{base + 2 * antenna}"


def ue_uplink_endpoint(ue_number):
    return _endpoint(
        f"SRSRAN_ZMQ_UE{ue_number}_UPLINK_ENDPOINT",
        ue_zmq_addr(),
        _env_int("SRSRAN_ZMQ_UE_UPLINK_BASE_PORT", 2100) + ue_number,
    )


def ue_downlink_endpoint(ue_number):
    return _endpoint(
        f"SRSRAN_ZMQ_UE{ue_number}_DOWNLINK_ENDPOINT",
        ue_zmq_addr(),
        _env_int("SRSRAN_ZMQ_UE_DOWNLINK_BASE_PORT", 2200) + ue_number,
    )


def ue_device_args(ue_number, base_srate="23.04e6"):
    return (
        f"tx_port={ue_uplink_endpoint(ue_number)},"
        f"rx_port={ue_downlink_endpoint(ue_number)},"
        f"base_srate={base_srate}"
    )
