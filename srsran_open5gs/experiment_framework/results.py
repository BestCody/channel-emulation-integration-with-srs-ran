#!/usr/bin/env python3

import hashlib
import json
import os
import pathlib
import tempfile
from datetime import datetime, timezone


def atomic_write_text(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def sha256_file(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def expected_result_layout(result_root, study_id):
    base = pathlib.Path(result_root) / study_id / "<UTC-run-id>"
    return [
        str(base / "resolved-study.json"),
        str(base / "pre-pilot-baseline"),
        str(base / "monitoring/amf-memory.jsonl"),
        str(base / "provenance/software-versions.json"),
        str(base / "provenance/source-checksums.sha256"),
        str(base / "trials/<condition-id>/trial-001/summary.json"),
        str(base / "trials/<condition-id>/trial-001/condition/channel"),
        str(base / "trials/<condition-id>/trial-001/condition/traffic"),
        str(base / "trials/<condition-id>/trial-001/condition/monitoring"),
        str(base / "trials/<condition-id>/trial-001/condition/logs"),
        str(base / "trials/<condition-id>/trial-001/restoration"),
        str(base / "post-pilot-baseline"),
        str(base / "summary/trials.csv"),
        str(base / "summary/conditions.json"),
        str(base / "summary/moving-positions.csv"),
        str(base / "summary/resource-samples.csv"),
        str(base / "summary/gpu-samples.csv"),
        str(base / "summary/amf-memory.csv"),
        str(base / "summary/failures.csv"),
        str(base / "summary/plots"),
        str(base / "study-checksums.sha256"),
    ]


class ResultStore:
    def __init__(self, result_root, study_id, *, run_id=None, create=True):
        result_root = pathlib.Path(result_root).resolve()
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.root = result_root / study_id / self.run_id
        if create:
            self.root.mkdir(parents=True, exist_ok=False)
            for name in ("provenance", "trials", "summary", "summary/plots"):
                (self.root / name).mkdir(parents=True, exist_ok=True)

    def trial(self, condition_id, trial_number):
        path = self.root / "trials" / condition_id / f"trial-{trial_number:03d}"
        path.mkdir(parents=True, exist_ok=False)
        for name in (
            "condition/channel",
            "condition/traffic",
            "condition/monitoring",
            "condition/logs",
            "restoration",
        ):
            (path / name).mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, relative_path, value):
        write_json(self.root / relative_path, value)

    def write_checksums(self):
        rows = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.name not in {"study-checksums.sha256"}:
                rows.append(f"{sha256_file(path)}  {path.relative_to(self.root)}")
        atomic_write_text(self.root / "study-checksums.sha256", "\n".join(rows) + "\n")
