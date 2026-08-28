#!/usr/bin/env python3

import os


def _required_env(name):
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def gnb_zmq_addr():
    return _required_env("SRSRAN_GNB_ZMQ_ADDR")


def ue_zmq_addr():
    return _required_env("SRSRAN_UE_ZMQ_ADDR")


def gnb_downlink_endpoint():
    return f"tcp://{gnb_zmq_addr()}:2000"


def gnb_uplink_endpoint():
    return f"tcp://{ue_zmq_addr()}:2001"


def ue_uplink_endpoint(ue_number):
    return f"tcp://{ue_zmq_addr()}:{2100 + ue_number}"


def ue_downlink_endpoint(ue_number):
    return f"tcp://{ue_zmq_addr()}:{2200 + ue_number}"
