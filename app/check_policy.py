"""Validate config/policy.yaml against the rest of the config: `python -m app.check_policy`.

CI runs it on every pull request, so a policy change that doesn't parse, names a tier
limits.yaml lacks, or leaves a routable model without a data-handling rule can't merge.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from app import config  # importing it loads and cross-checks every file
    except Exception as exc:
        print(f"policy check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    p = config.access
    print(
        f"policy {p.version}: {len(p.group_roles)} groups, {len(p.role_tiers)} roles, "
        f"{len(p.models)} model rules, {len(p.connectors)} connectors"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
