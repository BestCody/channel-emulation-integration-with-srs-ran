#!/usr/bin/env python3

import argparse
import json
import signal
import socket
import time


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.settimeout(0.5)
    receiver.bind((args.bind, args.port))
    with open(args.output, "w", encoding="utf-8") as output:
        while running:
            try:
                payload, source = receiver.recvfrom(1_048_576)
            except socket.timeout:
                continue
            text = payload.decode("utf-8")
            record = {
                "captured_at_ns": time.time_ns(),
                "payload": json.loads(text),
                "source": source[0],
            }
            output.write(json.dumps(record, sort_keys=True) + "\n")
            output.flush()
    receiver.close()


if __name__ == "__main__":
    main()
