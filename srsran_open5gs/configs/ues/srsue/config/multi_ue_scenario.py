#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0

import signal
import threading
from argparse import ArgumentParser

from gnuradio import blocks
from gnuradio import gr
from gnuradio import zeromq

from radio_endpoints import gnb_downlink_endpoint
from radio_endpoints import gnb_uplink_endpoint
from radio_endpoints import ue_downlink_endpoint
from radio_endpoints import ue_uplink_endpoint


class MultiUeScenario(gr.top_block):
    def __init__(self, num_ues):
        gr.top_block.__init__(self, "srsRAN_multi_UE")

        zmq_timeout = 100
        zmq_hwm = -1
        samp_rate = 23040000
        slow_down_ratio = 1

        self.zeromq_req_source_0 = zeromq.req_source(
            gr.sizeof_gr_complex,
            1,
            gnb_downlink_endpoint(),
            zmq_timeout,
            False,
            zmq_hwm,
        )
        self.zeromq_rep_sink_0_1 = zeromq.rep_sink(
            gr.sizeof_gr_complex,
            1,
            gnb_uplink_endpoint(),
            zmq_timeout,
            False,
            zmq_hwm,
        )

        self.zeromq_req_sources = []
        self.zeromq_rep_sinks = []
        self.blocks_throttle = blocks.throttle(
            gr.sizeof_gr_complex,
            samp_rate / slow_down_ratio,
            True,
        )
        self.blocks_add_xx = blocks.add_vcc(1)

        for i in range(num_ues):
            ue_number = i + 1
            req_source = zeromq.req_source(
                gr.sizeof_gr_complex,
                1,
                ue_uplink_endpoint(ue_number),
                zmq_timeout,
                False,
                zmq_hwm,
            )
            rep_sink = zeromq.rep_sink(
                gr.sizeof_gr_complex,
                1,
                ue_downlink_endpoint(ue_number),
                zmq_timeout,
                False,
                zmq_hwm,
            )
            self.zeromq_req_sources.append(req_source)
            self.zeromq_rep_sinks.append(rep_sink)
            self.connect((req_source, 0), (self.blocks_add_xx, i))
            self.connect((self.blocks_throttle, 0), (rep_sink, 0))

        self.connect((self.blocks_add_xx, 0), (self.zeromq_rep_sink_0_1, 0))
        self.connect((self.zeromq_req_source_0, 0), (self.blocks_throttle, 0))


def main():
    parser = ArgumentParser(description="srsRAN multi-UE setup")
    parser.add_argument("-n", "--num-ues", type=int, required=True)
    args = parser.parse_args()

    flowgraph = MultiUeScenario(args.num_ues)
    stop_event = threading.Event()

    def request_stop(sig=None, frame=None):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    flowgraph.start()
    try:
        stop_event.wait()
    finally:
        flowgraph.stop()
        flowgraph.wait()


if __name__ == '__main__':
    main()
