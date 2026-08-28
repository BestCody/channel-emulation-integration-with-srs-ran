#!/usr/bin/env python3

import os
import pathlib
import sys
import time


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_CONFIG = REPO_ROOT / "configs" / "ues" / "srsue-live" / "config"
sys.path.insert(0, str(LIVE_CONFIG))

from channel_protocol import PROTOCOL_VERSION
from channel_protocol import decode_message
from channel_protocol import encode_message


class ChannelClient:
    def __init__(self, endpoint=None, timeout_ms=5000, stream_endpoint=None):
        endpoint = endpoint or os.environ.get("CHANNEL_CONTROL_ENDPOINT", "tcp://127.0.0.1:5555")
        stream_endpoint = stream_endpoint or os.environ.get("CHANNEL_STREAM_ENDPOINT", "tcp://127.0.0.1:5556")
        import zmq

        self.zmq = zmq
        self.endpoint = endpoint
        self.stream_endpoint = stream_endpoint
        self.timeout_ms = timeout_ms
        self.context = zmq.Context()
        self.socket = None
        self.stream_socket = None
        self.connect()

    def connect(self):
        if self.socket is not None:
            self.socket.close()
        self.socket = self.context.socket(self.zmq.REQ)
        self.socket.linger = 0
        self.socket.connect(self.endpoint)
        if self.stream_socket is not None:
            self.stream_socket.close()
        self.stream_socket = self.context.socket(self.zmq.PUSH)
        self.stream_socket.linger = 0
        self.stream_socket.connect(self.stream_endpoint)

    def stream(self, message):
        self.stream_socket.send(encode_message(message))

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.stream_socket is not None:
            self.stream_socket.close()
            self.stream_socket = None
        self.context.term()

    def request(self, message, raise_on_error=True):
        started = time.perf_counter_ns()
        self.socket.send(encode_message(message))
        if self.socket.poll(self.timeout_ms) == 0:
            self.connect()
            raise TimeoutError(
                f"no response after {self.timeout_ms} ms"
            )
        response = decode_message(self.socket.recv())
        response["request_rtt_ms"] = (
            time.perf_counter_ns() - started
        ) / 1_000_000.0
        if raise_on_error and response.get("msg_type") == "error":
            raise RuntimeError(response.get("error", "update failed"))
        return response

    def get_config(self):
        return self.request(
            {"version": PROTOCOL_VERSION, "msg_type": "config_request"}
        )

    def get_status(self):
        return self.request(
            {"version": PROTOCOL_VERSION, "msg_type": "status_request"}
        )
