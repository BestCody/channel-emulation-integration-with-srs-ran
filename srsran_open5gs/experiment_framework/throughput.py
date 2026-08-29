import json


def parse_iperf3_json(text, direction):
    data = json.loads(text)
    if data.get("error"):
        return {
            "status": "failed",
            "error": str(data["error"]),
            "direction": direction,
            "megabits_per_second": 0.0,
            "bytes": 0,
            "seconds": 0.0,
            "retransmits": None,
            "sender_megabits_per_second": 0.0,
            "receiver_megabits_per_second": 0.0,
        }
    try:
        received = data["end"]["sum_received"]
        sent = data["end"]["sum_sent"]
        received_bps = float(received["bits_per_second"])
        sent_bps = float(sent["bits_per_second"])
        received_bytes = int(received["bytes"])
        received_seconds = float(received["seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("iperf3 result is incomplete") from error
    return {
        "status": "passed",
        "direction": direction,
        "megabits_per_second": received_bps / 1_000_000.0,
        "bytes": received_bytes,
        "seconds": received_seconds,
        "retransmits": sent.get("retransmits"),
        "sender_megabits_per_second": sent_bps / 1_000_000.0,
        "receiver_megabits_per_second": received_bps / 1_000_000.0,
    }
