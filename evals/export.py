"""Write the files the shared eval action reads, from this gateway's own config (ADR 0035).

    python -m evals.export --current fast --candidate anthropic/claude-sonnet-5-5 --out build/eval

The comparison itself is done by model-eval-action, which calls the two models through
the running gateway's API like any other client. It knows nothing about this repository,
so three things are handed to it:

    golden.yaml   the golden tasks, with tool names as a model sees them
    tools.json    the gateway's connector tools as function definitions
    prices.yaml   input and output prices for the two models, from config/pricing.yaml

Before anything is written, both models must be in config/models.yaml and have a
data-handling rule in config/policy.yaml (exit 2 otherwise): model turn-up has an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from evals.run_evals import GOLDEN, load_golden, turn_up_check


def golden_for_models(path: Path = GOLDEN) -> dict[str, Any]:
    """The golden file with `connector.tool` written as the function name a model is
    offered (`connector__tool`): the action compares names as they come back."""
    from app import connectors

    load_golden(path)  # a file this gateway's own harness would refuse is not exported
    raw = yaml.safe_load(path.read_text())
    for task in raw["tasks"]:
        for check in task["checks"]:
            if check.get("tool_call"):
                connector, _, tool = str(check["tool_call"]).partition(".")
                if tool:
                    check["tool_call"] = f"{connector}{connectors.TOOL_SEPARATOR}{tool}"
    return {"tasks": raw["tasks"]}


def prices_for(models: list[str]) -> dict[str, Any]:
    """Each model as it will be named in the request → the price of the first target
    behind it. An alias is priced as its primary: a fallback would cost something else."""
    from app import config

    out: dict[str, Any] = {}
    for name in dict.fromkeys(models):
        price = config.pricing.models.get(config.registry.resolve(name)[0])
        if price is not None and price.input is not None and price.output is not None:
            out[name] = {"input": price.input, "output": price.output}
    return {"models": out}


def export(out: Path, models: list[str], golden: Path = GOLDEN) -> list[Path]:
    from app import connectors

    out.mkdir(parents=True, exist_ok=True)
    files = {
        "golden.yaml": yaml.safe_dump(golden_for_models(golden), sort_keys=False),
        "tools.json": json.dumps(connectors.tool_definitions(), indent=2),
        "prices.yaml": yaml.safe_dump(prices_for(models), sort_keys=False),
    }
    for name, text in files.items():
        (out / name).write_text(text)
    return [out / name for name in files]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--current", required=True, help="the model in use today")
    parser.add_argument("--candidate", required=True, help="the model being considered")
    parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    args = parser.parse_args(argv)
    for name in dict.fromkeys((args.current, args.candidate)):
        if problem := turn_up_check(name):
            print(f"cannot evaluate: {problem}", file=sys.stderr)
            return 2
    for path in export(args.out, [args.current, args.candidate], args.golden):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
