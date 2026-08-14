"""`python -m cua` — the CLI surface (DESIGN §12).

Subcommands land as they are built. Ones that are not implemented yet exit 2 with
a message naming the day they arrive, rather than failing with a traceback or —
worse — silently doing nothing.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .contracts import ContractError, load_artifact, validate_result

ROOT = Path(__file__).resolve().parents[1]

NOT_YET = 2


def _not_implemented(what, when):
    print(f"'{what}' is not implemented yet ({when}).", file=sys.stderr)
    print("Built so far: `target_app serve`, `validate`.", file=sys.stderr)
    return NOT_YET


def cmd_target_app(args):
    if args.action == "serve":
        from target_app import serve
        print(f"Target app on http://{args.host}:{args.port}  (Ctrl+C to stop)")
        serve(host=args.host, port=args.port)
    return 0


def cmd_validate(args):
    """Validate an artifact or a result envelope against its contract."""
    failures = 0
    for path in args.paths:
        try:
            if args.kind == "result":
                with open(path, encoding="utf-8") as handle:
                    validate_result(json.load(handle))
                print(f"OK    {path}  (result envelope)")
            else:
                artifact = load_artifact(path)
                capability = artifact["capability"]
                print(f"OK    {path}  ({capability['name']} v{capability['version']}, "
                      f"{len(artifact['steps'])} steps)")
        except ContractError as error:
            failures += 1
            print(f"FAIL  {path}", file=sys.stderr)
            print(error, file=sys.stderr)
        except (OSError, json.JSONDecodeError) as error:
            failures += 1
            print(f"FAIL  {path}: {error}", file=sys.stderr)
    return 1 if failures else 0


def cmd_discover(args):
    """One real LLM-driven run against a live surface, recorded to /evidence."""
    from anthropic import Anthropic
    from playwright.sync_api import sync_playwright

    from . import config
    from .agent import DiscoveryLoop
    from .evidence import RunEvidence
    from .executor import BrowserSurface
    from .policy import Allowlist, PolicyViolation

    # Cheapest checks first, in cost order: an off-allowlist target, a missing key
    # or a malformed capability name should all fail before a browser window opens
    # or a single token is billed.
    try:
        Allowlist.from_file().check_origin(args.target)
    except PolicyViolation as violation:
        print(f"Refusing to start: {violation}", file=sys.stderr)
        return 1
    if not re.match(r"^[a-z][a-z0-9_]*$", args.save_as):
        print(f"Refusing to start: --save-as '{args.save_as}' is not snake_case; "
              f"it becomes the capability name in the artifact contract.", file=sys.stderr)
        return 1

    client = Anthropic(api_key=config.api_key())
    evidence = RunEvidence()
    print(f"Discovery run {evidence.run_id}  model={config.model()}")
    print(f"Goal: {args.goal}")

    with sync_playwright() as playwright:
        surface = BrowserSurface(playwright, headless=args.headless)
        try:
            loop = DiscoveryLoop(surface, client, evidence=evidence,
                                 capability_name=args.save_as)
            result = loop.run(args.goal, args.target)
        finally:
            surface.close()

    print(f"\nStatus: {result.status}  ({result.llm_call_count} model calls, "
          f"{len(result.steps)} steps)")
    if result.outputs:
        print("Outputs:")
        for name, value in result.outputs.items():
            print(f"  {name} = {value!r}")
    print(f"Evidence: {result.evidence_path}")

    if not result.succeeded:
        payload = result.envelope.get("payload", {})
        print(f"\n{result.status}: {payload.get('observed') or payload.get('detail', '')}",
              file=sys.stderr)
        return 1

    return _distil(args, evidence, result)


def _distil(args, evidence, result):
    """Compile the recorded run into a capability artifact."""
    import yaml

    from .contracts import ContractError, save_artifact
    from .distill import DistillationError, distil, outcomes_for
    from .evidence import utc_now
    from .policy import RiskPolicy
    from .policy.allowlist import DEFAULT_POLICY_PATH

    fingerprint = result.envelope.get("app_fingerprint_observed") or "unknown@unknown"
    app = fingerprint.split("@")[0]
    policy_config = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8")) or {}

    try:
        artifact = distil(
            evidence.read_trace(),
            capability_name=args.save_as,
            app=app,
            app_fingerprint=fingerprint,
            recorded_at=utc_now(),
            run_id=evidence.run_id,
            outcomes=outcomes_for(app),
            risk_policy=RiskPolicy.from_config(policy_config),
        )
    except (DistillationError, ContractError) as error:
        print(f"\nDistillation failed:\n{error}", file=sys.stderr)
        return 1

    path = save_artifact(artifact, ROOT / "capabilities" / f"{args.save_as}.json")
    evidence.write_json("artifact.json", artifact)

    print(f"\nCapability: {path}")
    print(f"  inputs:  {', '.join(artifact['inputs']) or '(none)'}")
    print(f"  outputs: {', '.join(artifact['outputs']) or '(none)'}")
    print(f"  steps:   {len(artifact['steps'])}  "
          f"({', '.join(sorted({s['risk'] for s in artifact['steps']}))})")
    print(f"  outcomes declared: {len(artifact['expected_outcomes'])}")
    return 0


def cmd_replay(_args):
    return _not_implemented("replay", "Day 3")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cua",
        description="Computer-use automation: discover a flow once, replay it deterministically.")
    parser.add_argument("--version", action="version", version=f"cua {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    target = subcommands.add_parser("target_app", help="run the fake operator portal")
    target.add_argument("action", choices=["serve"])
    target.add_argument("--host", default="127.0.0.1")
    target.add_argument("--port", type=int, default=5000)
    target.set_defaults(func=cmd_target_app)

    validate = subcommands.add_parser(
        "validate", help="check an artifact or result against its contract")
    validate.add_argument("paths", nargs="+")
    validate.add_argument("--kind", choices=["artifact", "result"], default="artifact")
    validate.set_defaults(func=cmd_validate)

    discover = subcommands.add_parser("discover", help="LLM-driven discovery run")
    discover.add_argument("--goal", required=True)
    discover.add_argument("--target", required=True, help="base URL of the application")
    discover.add_argument("--save-as", required=True, help="capability name (snake_case)")
    discover.add_argument("--headless", action="store_true",
                          help="run without a visible window (headed by default, "
                               "since the handoff seam needs a window a human can take over)")
    discover.set_defaults(func=cmd_discover)

    replay = subcommands.add_parser("replay", help="execute a saved artifact (Day 3)")
    replay.add_argument("capability")
    replay.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    replay.add_argument("--approve-mutations", action="store_true")
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
