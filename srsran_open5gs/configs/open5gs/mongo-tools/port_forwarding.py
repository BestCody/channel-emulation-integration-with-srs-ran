import socket
import subprocess
import time

NAMESPACE = "open5gs"


def wait_for_mongodb(process, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"kubectl port-forward exited with {return_code}"
            )
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", 27017)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError("MongoDB port-forward was not ready in 15 seconds")


def run_with_port_forwarding(script):
    process = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "service/mongodb",
            "-n",
            NAMESPACE,
            "27017:27017",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_mongodb(process)
        script()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
