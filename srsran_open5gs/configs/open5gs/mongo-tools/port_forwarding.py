import subprocess
import time
from logger import log

NAMESPACE = "open5gs"


def run_with_port_forwarding(script):
    """Forward the MongoDB service to the host."""
    try:
        port_forward_command = [
            "kubectl",
            "port-forward",
            "service/mongodb",
            "-n",
            NAMESPACE,
            "27017:27017",
        ]
        port_forward_process = subprocess.Popen(port_forward_command)
        time.sleep(5)

        script()

    except subprocess.CalledProcessError as e:
        log.warning(f"Error occurred during port forwarding: {e}")

    finally:
        if port_forward_process.poll() is None:
            port_forward_process.terminate()
            port_forward_process.wait()
