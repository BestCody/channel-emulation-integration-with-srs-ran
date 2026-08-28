#!/usr/bin/env python3

import threading
import time

from channel_protocol import (
    MAX_DELAY,
    MAX_TAPS,
    PROTOCOL_VERSION,
    decode_message,
    encode_message,
    parse_update,
)


def block_status(block):
    return {
        "sample_count": int(block.sample_count()),
        "update_count": int(block.update_count()),
        "tap_count": int(block.tap_count()),
    }


def _block_matrix(blocks, label):
    # Blocks are indexed as [ue][bs_antenna].
    matrix = [list(row) for row in blocks]
    if not matrix or not matrix[0]:
        raise ValueError(f"at least one {label} channel is required")
    width = len(matrix[0])
    if any(len(row) != width for row in matrix):
        raise ValueError(f"{label} channel matrix must be rectangular")
    return matrix


class ChannelControlServer:
    def __init__(
        self,
        bind_endpoint,
        downlinks,
        uplinks,
        sample_rate,
        stream_endpoint="tcp://0.0.0.0:5556",
    ):
        self.bind_endpoint = bind_endpoint
        self.stream_endpoint = stream_endpoint
        self.downlinks = _block_matrix(downlinks, "downlink")
        self.uplinks = _block_matrix(uplinks, "uplink")
        if len(self.downlinks) != len(self.uplinks):
            raise ValueError("downlink and uplink channel counts must match")
        if len(self.downlinks[0]) != len(self.uplinks[0]):
            raise ValueError("downlink and uplink antenna counts must match")
        self.num_ues = len(self.downlinks)
        self.gnb_antennas = len(self.downlinks[0])
        self.sample_rate = float(sample_rate)
        self._stop_event = threading.Event()
        self._thread = None
        self._transaction_lock = threading.Lock()
        self.accepted_updates = 0
        self.rejected_updates = 0
        self.last_set_us = 0.0
        self.last_accepted_sequence = 0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="channel-control",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def config_response(self):
        return {
            "version": PROTOCOL_VERSION,
            "msg_type": "config_response",
            "backend": "dense-cir-stream",
            "sample_rate": self.sample_rate,
            "max_taps": MAX_TAPS,
            "max_delay": MAX_DELAY,
            "directions": ["both", "downlink", "uplink"],
            "per_symbol_channels": True,
            "num_ues": self.num_ues,
            "gnb_antennas": self.gnb_antennas,
        }

    def status(self):
        return {
            "version": PROTOCOL_VERSION,
            "msg_type": "status_response",
            "accepted_updates": self.accepted_updates,
            "rejected_updates": self.rejected_updates,
            "last_set_us": self.last_set_us,
            "last_accepted_sequence": self.last_accepted_sequence,
            "num_ues": self.num_ues,
            "gnb_antennas": self.gnb_antennas,
            "downlink": [
                [block_status(block) for block in row]
                for row in self.downlinks
            ],
            "uplink": [
                [block_status(block) for block in row]
                for row in self.uplinks
            ],
        }

    def _ue_slice(self, ue_index):
        if ue_index == 0:
            return range(self.num_ues)
        if 1 <= ue_index <= self.num_ues:
            return (ue_index - 1,)
        raise ValueError(
            f"ue_index {ue_index} is outside 0..{self.num_ues}"
        )

    def _bs_slice(self, bs_index):
        if bs_index == 0:
            return range(self.gnb_antennas)
        if 1 <= bs_index <= self.gnb_antennas:
            return (bs_index - 1,)
        raise ValueError(
            f"bs_index {bs_index} is outside 0..{self.gnb_antennas}"
        )

    def _selected_blocks(self, direction, ue_index, bs_index):
        blocks = []
        for ue in self._ue_slice(ue_index):
            for bs in self._bs_slice(bs_index):
                if direction in ("both", "downlink"):
                    blocks.append(self.downlinks[ue][bs])
                if direction in ("both", "uplink"):
                    blocks.append(self.uplinks[ue][bs])
        return blocks

    def apply_update(self, update):
        coefficients = tuple(tap.coefficient for tap in update.taps)
        delays = tuple(tap.delay for tap in update.taps)
        with self._transaction_lock:
            started = time.perf_counter_ns()
            for block in self._selected_blocks(
                update.direction, update.ue_index, update.bs_index
            ):
                block.set_channel(coefficients, delays, update.noise_sigma)
            self.last_set_us = (time.perf_counter_ns() - started) / 1000.0
            self.last_accepted_sequence = update.sequence
            self.accepted_updates += 1

    def _handle_request(self, request):
        message_type = request.get("msg_type")
        if request.get("version") != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        if message_type == "config_request":
            return self.config_response()
        if message_type == "status_request":
            return self.status()
        raise ValueError(f"unsupported message type: {message_type}")

    def _process_stream_frame(self, payload):
        try:
            update = parse_update(decode_message(payload))
            self.apply_update(update)
            return True
        except Exception:
            self.rejected_updates += 1
            return False

    def _run(self):
        import zmq

        context = zmq.Context()
        control = context.socket(zmq.REP)
        control.linger = 0
        control.bind(self.bind_endpoint)
        stream = context.socket(zmq.PULL)
        stream.linger = 0
        stream.bind(self.stream_endpoint)
        poller = zmq.Poller()
        poller.register(control, zmq.POLLIN)
        poller.register(stream, zmq.POLLIN)
        print(
            f"Channel control on {self.bind_endpoint}, "
            f"CIR stream on {self.stream_endpoint}",
            flush=True,
        )

        try:
            while not self._stop_event.is_set():
                events = dict(poller.poll(timeout=100))
                if stream in events:
                    self._process_stream_frame(stream.recv())
                if control in events:
                    try:
                        request = decode_message(control.recv())
                        response = self._handle_request(request)
                    except Exception as error:
                        response = {
                            "version": PROTOCOL_VERSION,
                            "msg_type": "error",
                            "error": str(error),
                        }
                    control.send(encode_message(response))
        finally:
            control.close()
            stream.close()
            context.term()
