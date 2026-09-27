"""
Export a finished task as a Final Cut Pro project (.fcpxml).

New renders export automatically; use this to re-export a task after the fact.

Usage:
    uv run python export_fcpxml.py <task-id or task directory>
    uv run python export_fcpxml.py --latest
"""

import argparse
import glob
import json
import os
import sys

from app.services import fcpxml
from app.utils import utils


def _resolve_task_dir(target: str | None, latest: bool) -> str:
    if latest:
        tasks_root = os.path.dirname(utils.task_dir("placeholder"))
        candidates = [
            d for d in glob.glob(os.path.join(tasks_root, "*")) if os.path.isdir(d)
        ]
        if not candidates:
            sys.exit("No tasks found.")
        return max(candidates, key=os.path.getmtime)
    if os.path.isdir(target):
        return os.path.abspath(target)
    return utils.task_dir(target)


def _project_name(task_dir: str, index: str) -> str:
    try:
        with open(os.path.join(task_dir, "script.json"), encoding="utf-8") as f:
            subject = json.load(f)["params"]["video_subject"].strip()
    except (OSError, KeyError, ValueError, AttributeError):
        subject = os.path.basename(task_dir)
    return f"{subject[:60]} ({index})" if index != "1" else subject[:60]


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("task", nargs="?", help="task id or task directory")
    parser.add_argument("--latest", action="store_true", help="use the newest task")
    args = parser.parse_args()
    if not args.task and not args.latest:
        parser.error("pass a task id/directory or --latest")

    task_dir = _resolve_task_dir(args.task, args.latest)
    timelines = sorted(glob.glob(os.path.join(task_dir, "timeline-*.json")))
    if not timelines:
        sys.exit(
            f"No timeline found in {task_dir}.\n"
            "Only videos rendered after the Final Cut export was added can be exported."
        )
    for timeline in timelines:
        index = os.path.basename(timeline)[len("timeline-") : -len(".json")]
        combined = os.path.join(task_dir, f"combined-{index}.mp4")
        print(fcpxml.export_video_project(combined, _project_name(task_dir, index)))


if __name__ == "__main__":
    main()
