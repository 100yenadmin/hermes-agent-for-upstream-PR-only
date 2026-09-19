"""Invoke the real Hermes CLI with the evaluation network guard installed."""

import argparse
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.astra_subscription.live_guard import LiveBudget


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--scenario", required=True)
    args, cli_args = parser.parse_known_args()
    if cli_args[:1] == ["--"]:
        cli_args = cli_args[1:]
    LiveBudget(args.ledger, args.scenario).install()
    sys.argv = ["hermes", *cli_args]
    runpy.run_module("hermes_cli.main", run_name="__main__")


if __name__ == "__main__":
    main()
