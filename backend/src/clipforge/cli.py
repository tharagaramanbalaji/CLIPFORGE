"""Small operator CLI.  `python -m clipforge.cli env` prints the GPU/FFmpeg environment report."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("env", help="print GPU and FFmpeg capabilities as JSON")
    args = parser.parse_args(argv)

    if args.cmd == "env":
        from clipforge.workers.tasks import environment_report

        report = environment_report()
        print(json.dumps(report, indent=2))
        return 0 if report["ffmpeg"]["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
