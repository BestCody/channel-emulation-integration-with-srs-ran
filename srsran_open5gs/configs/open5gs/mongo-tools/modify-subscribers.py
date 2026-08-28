from open5gs import Open5GS
from port_forwarding import run_with_port_forwarding
from logger import log
import argparse
import json

MONGO_URI = "localhost"
MONGO_PORT = 27017
DATA_DIR = "../data"


def add_subscribers():
    with open(DATA_DIR + "/subscribers.json", "r") as file:
        subscribers = json.loads(file.read())
    open5gs = Open5GS(MONGO_URI, MONGO_PORT)
    for subscriber_name, subscriber_info in subscribers.items():
        open5gs.add_subscriber(subscriber_info)
        log.info(f"Registered {subscriber_name}")


def delete_subscribers():
    with open(DATA_DIR + "/subscribers.json", "r") as file:
        subscribers = json.loads(file.read())
    open5gs = Open5GS(MONGO_URI, MONGO_PORT)
    for subscriber_name, subscriber_info in subscribers.items():
        imsi = subscriber_info["imsi"]
        open5gs.delete_subscriber(imsi)
        log.info(f"Deleted {subscriber_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add or delete subscribers.")
    parser.add_argument(
        "action", choices=["add", "delete"], help="Specify action: add or delete"
    )
    args = parser.parse_args()

    if args.action == "add":
        run_with_port_forwarding(add_subscribers)
    elif args.action == "delete":
        run_with_port_forwarding(delete_subscribers)
