#!/usr/bin/env python3

import pathlib
import platform
import subprocess
from datetime import datetime, timezone

from .config import source_record
from .results import atomic_write_text, write_json


def version_commands(parameters):
    monitoring = parameters["monitoring"]
    commands = {
        "git": ["git", "--version"],
        "kubectl": ["kubectl", "version", "-o", "json"],
        "docker": ["docker", "--version"],
        "containerd": ["sudo", "ctr", "version"],
        "kernel": ["uname", "-a"],
        "sionna_environment": [
            parameters["host_python"],
            "-c",
            "import json,numpy,sionna,sionna.rt,torch; "
            "print(json.dumps({'sionna':sionna.__version__,'sionna_rt':sionna.rt.__version__,"
            "'torch':torch.__version__,'cuda':torch.version.cuda,'numpy':numpy.__version__},sort_keys=True))",
        ],
    }
    commands["nvidia_smi"] = [
        monitoring["nvidia_smi"],
        "--query-gpu=name,uuid,driver_version,memory.total",
        "--format=csv",
    ]
    return commands


def run_capture(command, cwd=None):
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        return {"command": command, "return_code": completed.returncode, "output": completed.stdout}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "return_code": None, "output": str(error)}


def collect_provenance(output_dir, repo_root, resolved_study, parameters):
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = pathlib.Path(repo_root)
    versions = {
        name: run_capture(command, repo_root)
        for name, command in version_commands(parameters).items()
    }
    versions["python"] = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }
    versions["created_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(output_dir / "software-versions.json", versions)
    atomic_write_text(output_dir / "git-status.txt", run_capture(["git", "status", "--short", "--branch"], repo_root)["output"])
    atomic_write_text(output_dir / "git-head.txt", run_capture(["git", "rev-parse", "HEAD"], repo_root)["output"])
    atomic_write_text(output_dir / "tracked-diff.patch", run_capture(["git", "diff", "--binary"], repo_root)["output"])
    artifacts = {}
    for record in resolved_study["parameter_configurations"]:
        artifacts[record["absolute_path"]] = record
    for condition in resolved_study["conditions"]:
        for record in condition["input_artifacts"]:
            artifacts[record["absolute_path"]] = record
    source_roots = [
        repo_root / "experiment_framework",
        repo_root / "experiments",
        repo_root / "channel_emulation",
        repo_root / "configs/ues/srsue",
        repo_root / "configs/ues/srsue-live",
        repo_root / "configs/srsRAN",
        repo_root / "containers",
        repo_root / "gr-sionna-channel",
    ]
    source_files = [
        repo_root / "bin/evaluation-experiment.py",
        repo_root / "bin/baseline.sh",
    ]
    for root in source_roots:
        source_files.extend(
            path for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
    for path in source_files:
        record = source_record(path)
        artifacts[record["absolute_path"]] = record
    write_json(output_dir / "input-artifacts.json", list(artifacts.values()))
    lines = [f"{item['sha256']}  {item['path']}" for item in sorted(artifacts.values(), key=lambda value: value["path"])]
    atomic_write_text(output_dir / "source-checksums.sha256", "\n".join(lines) + "\n")
    return versions
