#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0

from gnuradio import blocks
from gnuradio import gr
import sys
import signal
from argparse import ArgumentParser
from gnuradio import zeromq

from radio_endpoints import gnb_downlink_endpoint
from radio_endpoints import gnb_uplink_endpoint
from radio_endpoints import ue_downlink_endpoint
from radio_endpoints import ue_uplink_endpoint


class multi_ue_scenario(gr.top_block):
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
        self.blocks_throttle = blocks.throttle(gr.sizeof_gr_complex*1, samp_rate / slow_down_ratio, True)
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
    parser = ArgumentParser(description='srsRAN_multi_UE setup')
    parser.add_argument('-n', '--num-ues', type=int, required=True, help='Number of UEs')
    args = parser.parse_args()

    tb = multi_ue_scenario(args.num_ues)

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()

    try:
        input('Press Enter to quit: ')
    except EOFError:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
