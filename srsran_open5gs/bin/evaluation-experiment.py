#!/usr/bin/env python3

import argparse
import json
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from experiment_framework.config import load_and_resolve_study, parse_overrides
from experiment_framework.condition_modes import study_plan
from experiment_framework.results import expected_result_layout, write_json
from experiment_framework.experiment_runner import PilotRunner
from experiment_framework.summarize import summarize_run


def parser():
    result = argparse.ArgumentParser(description="Research evaluation experiment framework")
    commands = result.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="validate and resolve a study without running it")
    resolve.add_argument("study")
    resolve.add_argument("--parameters", action="append", default=[])
    resolve.add_argument("--output", required=True)

    plan = commands.add_parser("plan", help="print the resolved execution plan without running it")
    plan.add_argument("study")
    plan.add_argument("--parameters", action="append", default=[])

    run = commands.add_parser("run", help="run the configured benchmark study")
    run.add_argument("study")
    run.add_argument("--parameters", action="append", default=[])
    run.add_argument("--namespace")
    run.add_argument(
        "--confirm-live",
        action="store_true",
        help="required acknowledgement that Kubernetes and radio processes will be changed",
    )

    summarize = commands.add_parser("summarize", help="regenerate tables and plots for an existing run")
    summarize.add_argument("run_directory")

    for command in (resolve, plan, run):
        command.add_argument(
            "--set", action="append", default=[], dest="param_overrides", metavar="KEY=VALUE",
            help="override a benchmark/study parameter, e.g. --set radio.ue_number=2",
        )
        command.add_argument(
            "--condition-set", action="append", default=[], dest="condition_overrides", metavar="KEY=VALUE",
            help="override a condition field, e.g. --condition-set propagation.specular_reflection=true",
        )
        command.add_argument(
            "--scene-set", action="append", default=[], dest="scene_overrides", metavar="KEY=VALUE",
            help="override a scene value, e.g. --scene-set antenna.polarization=V",
        )
        command.add_argument(
            "--profile-set", action="append", default=[], dest="profile_overrides", metavar="KEY=VALUE",
            help="override a measurement-profile value, e.g. --profile-set final_ping.count=10",
        )
    return result


def main():
    args = parser().parse_args()
    if args.command == "summarize":
        summary = summarize_run(args.run_directory)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    resolved = load_and_resolve_study(
        args.study,
        parameter_files=args.parameters,
        parameter_overrides=parse_overrides(args.param_overrides),
        condition_overrides=parse_overrides(args.condition_overrides),
        scene_overrides=parse_overrides(args.scene_overrides),
        profile_overrides=parse_overrides(args.profile_overrides),
    )
    if args.command == "resolve":
        write_json(args.output, resolved)
        print(args.output)
        return
    if args.command == "plan":
        print(json.dumps({
            "study": resolved,
            "plan": study_plan(resolved),
            "result_layout": expected_result_layout(
                resolved["result_root"],
                resolved["study_id"],
            ),
        }, indent=2, sort_keys=True))
        return
    if not args.confirm_live:
        raise SystemExit("run requires --confirm-live; no experiment was started")
    output = PilotRunner(resolved, namespace=args.namespace).run()
    print(output)


if __name__ == "__main__":
    main()
