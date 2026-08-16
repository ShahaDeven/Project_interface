"""`python -m cua` — the CLI surface (DESIGN §12).

Every subcommand validates in cost order: parameters, then policy, then a browser,
then a model. A mistake at the command line should cost milliseconds, not a browser
launch and a billed call.

Exit codes say which half went wrong, because a caller scripting around this needs
to tell them apart:

    0  the run completed — SUCCESS, or a BUSINESS_OUTCOME, which is an answer
    1  the run happened and did not finish — HARD_FAILURE, NEEDS_INTERVENTION
    2  the run never started — bad parameters, an off-allowlist target, an unknown
       capability, no browser installed. Nothing was touched and nothing was spent.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .contracts import ContractError, load_artifact, validate_result

ROOT = Path(__file__).resolve().parents[1]

INSTALL_HINT = "playwright install chromium"


class BrowserMissing(Exception):
    """The Playwright client is installed; the browser binary is not."""


def open_browser(playwright, headless):
    """Launch Chromium, or say what is missing instead of raising a stack trace.

    The separate browser download is the one setup step a reader is most likely to
    skip, and Playwright's own error is long and does not lead with the fix. Every
    other startup problem in this CLI — an off-allowlist target, a bad parameter, a
    malformed capability — exits with a sentence, so this one should too.
    """
    from .executor import BrowserSurface
    try:
        return BrowserSurface(playwright, headless=headless)
    except Exception as error:
        text = str(error)
        if "Executable doesn't exist" in text or "playwright install" in text:
            raise BrowserMissing(
                f"Chromium is not installed. `pip install` fetches the Python "
                f"client; the browser is a separate ~140MB download:\n\n"
                f"    {INSTALL_HINT}") from None
        raise


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
    from .hitl import TerminalConsole
    from .policy import Allowlist, PolicyViolation

    # Cheapest checks first, in cost order: an off-allowlist target, a missing key
    # or a malformed capability name should all fail before a browser window opens
    # or a single token is billed.
    try:
        Allowlist.from_file().check_origin(args.target)
    except PolicyViolation as violation:
        print(f"Refusing to start: {violation}", file=sys.stderr)
        return 2
    if not re.match(r"^[a-z][a-z0-9_]*$", args.save_as):
        print(f"Refusing to start: --save-as '{args.save_as}' is not snake_case; "
              f"it becomes the capability name in the artifact contract.", file=sys.stderr)
        return 2

    client = Anthropic(api_key=config.api_key())
    evidence = RunEvidence()
    print(f"Discovery run {evidence.run_id}  model={config.model()}")
    print(f"Goal: {args.goal}")

    with sync_playwright() as playwright:
        try:
            surface = open_browser(playwright, args.headless)
        except BrowserMissing as missing:
            print(f"Cannot start: {missing}", file=sys.stderr)
            return 2
        try:
            # `stuck` is §9's discovery trigger: with an operator present the run
            # can be unblocked and continue instead of ending there.
            loop = DiscoveryLoop(surface, client, evidence=evidence,
                                 capability_name=args.save_as,
                                 console=TerminalConsole())
            result = loop.run(args.goal, args.target)
        except config.MissingCredential as error:
            # Discovery cannot declare its secrets up front — it is discovering
            # them — so this one is caught rather than pre-flighted.
            print(f"\nCannot continue: {error}", file=sys.stderr)
            return 2
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


def cmd_replay(args):
    """Execute a saved capability with new inputs. No model in the loop."""
    from playwright.sync_api import sync_playwright

    from .evidence import RunEvidence
    from .hitl import TerminalConsole
    from .policy import Allowlist, PolicyViolation
    from .replay import InputError, ReplayEngine, RuntimeConfig, bind_inputs, parse_params

    path = ROOT / "capabilities" / f"{args.capability}.json"
    if not path.exists():
        print(f"No capability named '{args.capability}' in capabilities/", file=sys.stderr)
        return 2

    try:
        artifact = load_artifact(path)
    except ContractError as error:
        print(f"{path} is not a valid capability:\n{error}", file=sys.stderr)
        return 2

    # Cost order again: parameters, then policy, then credentials, then a browser.
    try:
        params = parse_params(args.param)
        inputs = bind_inputs(artifact, params)
        Allowlist.from_file().check_origin(args.target)
    except (InputError, PolicyViolation) as error:
        print(f"Refusing to start: {error}", file=sys.stderr)
        return 2

    # Checked here rather than at the keystroke that needs it: the capability
    # declares what it requires, so an unconfigured credential can cost
    # milliseconds instead of a browser launch and three completed steps.
    from . import config
    absent = config.missing_secrets(artifact["capability"].get("requires_secrets", []))
    if absent:
        listed = " and ".join(filter(None, [", ".join(absent[:-1]), absent[-1]]))
        print(f"Refusing to start: this capability needs {listed}, which "
              f"{'is' if len(absent) == 1 else 'are'} not configured.\n"
              f"Copy .env.example to .env — the operator credentials in it work as "
              f"they are, and no API key is needed to replay.", file=sys.stderr)
        return 2

    app = artifact["capability"]["recorded_against"]["app"]
    runtime = RuntimeConfig.load(app)
    if not runtime.profiled:
        print(f"warning: no runtime profile for '{app}'; error pages and session "
              f"expiry cannot be recognised", file=sys.stderr)

    # A pause needs both an operator to ask and a window for them to act in. Absent
    # either, the run still pauses and still reports it — the intervention just
    # stays terminal, which is the honest outcome for an unattended run.
    console = None if args.no_console else TerminalConsole()
    if console and not console.available():
        print("note: not a terminal, so an intervention will be reported rather "
              "than waited on", file=sys.stderr)
    elif console and args.headless:
        print("note: --headless leaves an operator nothing to take over; run headed "
              "if you expect to hand off", file=sys.stderr)

    evidence = RunEvidence()
    print(f"Replay {args.capability} v{artifact['capability']['version']}  "
          f"run {evidence.run_id}")
    print(f"Inputs: {inputs}")

    with sync_playwright() as playwright:
        try:
            surface = open_browser(playwright, args.headless)
        except BrowserMissing as missing:
            print(f"Cannot start: {missing}", file=sys.stderr)
            return 2
        try:
            engine = ReplayEngine(surface, artifact, evidence=evidence, runtime=runtime,
                                  approve_mutations=args.approve_mutations,
                                  screenshots=args.screenshots, chaos=args.chaos,
                                  console=console)
            result = engine.run(params, args.target)
        finally:
            surface.close()

    envelope = result.envelope
    print(f"\nStatus: {result.status}  "
          f"({envelope['steps_completed']}/{envelope['steps_total']} steps, "
          f"llm_call_count={envelope['llm_call_count']})")
    if envelope.get("drift_warning"):
        print(f"Drift: {envelope['drift_warning']}")
    for recovery in result.recoveries:
        # Surfaced but not promoted: the run recovered, so this is information for
        # whoever is watching, not a status the caller has to branch on.
        print(f"Recovered: {recovery['condition']} at step {recovery['step']} "
              f"({', '.join(f'{k}={v}' for k, v in recovery.items() if k not in ('condition', 'step'))})")
    for entry in envelope.get("intervention_record", {}).get("interventions", []):
        print(f"Handed over: step {entry['paused_at_step']} ({entry['reason']}) "
              f"-> {entry['decision']}, {entry['resolution']}")
    if result.outputs:
        print("Outputs:")
        for name, value in result.outputs.items():
            print(f"  {name} = {value!r}  ({type(value).__name__})")

    payload = envelope.get("payload", {})
    if result.status == "BUSINESS_OUTCOME":
        # Printed to stdout, not stderr, and exits 0: this is an answer, not an
        # error. A caller scripting around this system should be able to branch on
        # the outcome code without treating the run as broken.
        print(f"\nOutcome: {payload['outcome_code']}  "
              f"(detected at step {payload['detected_at_step']})")
        print(f"  {payload['detail']}")
    elif result.status == "HARD_FAILURE":
        print(f"\nFailed at step {payload['failed_at_step']} ({payload['action_attempted']})",
              file=sys.stderr)
        print(f"  expected: {payload['expected']}", file=sys.stderr)
        print(f"  observed: {payload['observed']}", file=sys.stderr)
    elif result.status == "NEEDS_INTERVENTION":
        print(f"\n{payload['reason']}: {payload['detail']}", file=sys.stderr)
        print(f"  requested: {payload['requested_action']}", file=sys.stderr)

    print(f"Evidence: {result.evidence_path}")
    return 0 if result.status in ("SUCCESS", "BUSINESS_OUTCOME") else 1


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

    replay = subcommands.add_parser("replay", help="execute a saved artifact, no LLM")
    replay.add_argument("capability")
    replay.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    replay.add_argument("--target", default="http://127.0.0.1:5000",
                        help="base URL the artifact's {base_url} resolves to")
    replay.add_argument("--approve-mutations", action="store_true",
                        help="standing approval for `mutating` steps; never covers "
                             "`irreversible` ones")
    replay.add_argument("--headless", action="store_true")
    replay.add_argument("--screenshots", choices=["failure", "all"], default="failure")
    replay.add_argument("--chaos", choices=["slow", "session", "dialog", "error"],
                        help="demo scaffolding: arm a runtime condition in the target "
                             "app from this run's own browser session, mid-flow")
    replay.add_argument("--no-console", action="store_true",
                        help="never pause for a human; report the intervention and "
                             "stop, as an unattended run would")
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
