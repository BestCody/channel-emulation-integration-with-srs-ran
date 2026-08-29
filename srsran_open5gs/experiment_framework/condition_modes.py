#!/usr/bin/env python3


from .settings import PORT_FORWARD, PORT_FORWARD_STREAM


def _port_forward_display():
    return f"{PORT_FORWARD}, {PORT_FORWARD_STREAM}"


def condition_plan(condition):
    port_forward = _port_forward_display()
    propagation = condition["propagation"]
    effects = ", ".join(f"{key}={value}" for key, value in sorted(propagation.items())) or "all RT effects off"
    actions = [
        "apply separate overlay",
        "confirm wrapper started no radio process",
        "start GNU Radio, gNB and UE once",
        "wait for random access, RRC and PDU session",
        "start CPU, GPU, AMF and process-identity monitoring",
        f"resolve scene {condition['scene']} with propagation {effects}",
        f"start kubectl port-forward on {port_forward}",
    ]
    return actions + [
        "run and validate the live moving-channel stream",
        "activate position zero before establishing movement epoch",
        "run trajectory positions at fixed targets without restarts",
        "skip late positions and record continuous ping",
    ]


def study_plan(resolved_study):
    actions = [
        "save original UE deployment, ConfigMap, image, pull policy and replicas",
        "start one continuous AMF monitor for the complete pilot",
    ]
    for condition in resolved_study["conditions"]:
        actions.append({
            "condition_id": condition["condition_id"],
            "trial_count": resolved_study["trials_per_condition"],
            "actions": condition_plan(condition),
            "after_success": "restore deployment and validate only; do not reconnect radio",
            "after_failure": "restore, run recovery check, then stop study",
        })
    actions.extend([
        "stop AMF monitor and verify no unsafe event occurred",
        "generate individual-trial tables, aggregate tables and SVG plots",
        "write checksums for the complete result tree",
    ])
    return actions
