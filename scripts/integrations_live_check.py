#!/usr/bin/env python3
"""Live verification of the owner integrations, against the real APIs.

The mocked layers prove the code's logic; only this proves the *credentials and
permissions* work -- a fine-grained GitHub token without ``actions: read``, or a
Vercel token scoped to the wrong team, is invisible to every offline test and
only fails here.

    python scripts/integrations_live_check.py                   # reads only
    python scripts/integrations_live_check.py --allow-mutations  # + dispatch + deploy hook
    python scripts/integrations_live_check.py --self-test        # offline: proves this checker
    python scripts/integrations_live_check.py --json

Mutations are opt-in and this is deliberate. ``workflow dispatch`` starts a real
CI run and the deploy hook triggers a real production deployment, so a check that
fired them by default would make "verify my tokens" a destructive command. Reads
run whenever credentials exist; writes need the flag.

Secrets never reach stdout. Every message is passed through the same redactor the
API uses, and the exit path re-scans the whole transcript before printing it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "backend"),):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from integrations import config as cfgmod          # noqa: E402
from integrations import http as httpmod           # noqa: E402
from integrations import redact as redactmod       # noqa: E402
from integrations.service import IntegrationService  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Result:
    def __init__(self, provider, operation, status, detail=""):
        self.provider = provider
        self.operation = operation
        self.status = status
        self.detail = detail

    def as_dict(self):
        return {"provider": self.provider, "operation": self.operation,
                "status": self.status, "detail": self.detail}

    def line(self, width=26):
        return f"{self.provider:<8} {self.operation:<{width}} {self.status}" \
               + (f"  {self.detail}" if self.detail else "")


def check_github_read(service, redact):
    """Reads only; changes nothing upstream."""
    try:
        runs = service.github_runs()
        return Result("GitHub", "GET workflow runs", PASS, f"{runs['count']} run(s)")
    except httpmod.IntegrationError as error:
        return Result("GitHub", "GET workflow runs", FAIL,
                      f"{error.code}: {redact(error.message)}")


def check_vercel_read(service, redact):
    """Reads only; changes nothing upstream."""
    try:
        deps = service.vercel_deployments()
        return Result("Vercel", "GET deployments", PASS,
                      f"{deps['count']} deployment(s)")
    except httpmod.IntegrationError as error:
        return Result("Vercel", "GET deployments", FAIL,
                      f"{error.code}: {redact(error.message)}")


def check_reads(service, redact):
    """Both reads. Kept as one call for the live test layer."""
    return [check_github_read(service, redact), check_vercel_read(service, redact)]


def _dispatch(service, redact, ref):
    try:
        dispatched = service.github_dispatch(ref=ref)
        return PASS, f"ref={dispatched['ref']}"
    except httpmod.IntegrationError as error:
        return FAIL, f"{error.code}: {redact(error.message)}"


def _hook(service, redact):
    try:
        hook = service.vercel_deploy()
        return Result("Vercel", "deploy hook", PASS,
                      f"id={hook.get('deployment_id')}")
    except httpmod.IntegrationError as error:
        return Result("Vercel", "deploy hook", FAIL,
                      f"{error.code}: {redact(error.message)}")


def check_mutations(service, redact, ref="main"):
    """The two write operations. Only called with --allow-mutations."""
    return [Result("GitHub", "workflow dispatch", *_dispatch(service, redact, ref)),
            _hook(service, redact)]


def skip_mutations(reason="needs --allow-mutations"):
    return [Result("GitHub", "workflow dispatch", SKIP, reason),
            Result("Vercel", "deploy hook", SKIP, reason)]


def unconfigured_checks(config):
    """Report what cannot be checked at all, instead of silently passing.

    A run that checks nothing and exits 0 is worse than a failure: it is the one
    outcome that reads as "verified" while proving nothing.
    """
    out = []
    if not config.github.configured:
        out.append(Result("GitHub", "GET workflow runs", SKIP,
                          "missing " + ", ".join(config.github.missing())))
    if not config.vercel.configured:
        out.append(Result("Vercel", "GET deployments", SKIP,
                          "missing " + ", ".join(
                              name for name in config.vercel.missing()
                              if name != "DEPLOY_HOOK_URL")))
    if config.vercel.configured and not config.vercel.hook_configured:
        out.append(Result("Vercel", "deploy hook", SKIP, "missing DEPLOY_HOOK_URL"))
    return out


def render(results, as_json=False):
    if as_json:
        return json.dumps([item.as_dict() for item in results], indent=2,
                          ensure_ascii=False)
    return "\n".join(item.line() for item in results)


def run(config=None, service=None, allow_mutations=False, ref="main"):
    config = config or cfgmod.load()
    service = service or IntegrationService(config)
    redact = redactmod.build_redactor(config.secrets())
    # Decided per operation, not all-or-nothing. The first version of this
    # skipped *everything* when any one provider was unconfigured, so a deployment
    # with a working GitHub token and no Vercel token reported that it had checked
    # nothing -- and silently stopped verifying GitHub on every run after that.
    skipped = {item.operation: item for item in unconfigured_checks(config)}
    results = [skipped["GET workflow runs"] if "GET workflow runs" in skipped
               else check_github_read(service, redact),
               skipped["GET deployments"] if "GET deployments" in skipped
               else check_vercel_read(service, redact)]
    if not allow_mutations:
        results.extend(skip_mutations())
        return results
    results.append(Result("GitHub", "workflow dispatch", *_dispatch(service, redact, ref)))
    if "deploy hook" in skipped:
        results.append(skipped["deploy hook"])
    else:
        results.append(_hook(service, redact))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allow-mutations", action="store_true",
                        help="also dispatch a workflow and fire the deploy hook "
                             "(both change real state)")
    parser.add_argument("--ref", default="main", help="branch to dispatch")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="offline: prove this checker's own logic on fakes")
    args = parser.parse_args(argv)

    config = cfgmod.load()
    if args.self_test:
        return self_test(config)

    if not config.owner.configured:
        print("WAHA_OWNER_TOKEN is not set; the /integrations page would refuse "
              "every call even with working provider tokens.")
    results = run(config=config, allow_mutations=args.allow_mutations, ref=args.ref)
    text = render(results, args.json)
    # Last line of defence: the transcript is scanned before it is printed, so a
    # redaction bug cannot leak a token through a "verified" report.
    for secret in config.secrets():
        if secret and len(secret) >= redactmod.MIN_SECRET_LENGTH:
            text = text.replace(secret, redactmod.REDACTED)
    print(text)
    failed = [item for item in results if item.status == FAIL]
    passed = [item for item in results if item.status == PASS]
    if not passed:
        print("\nnothing was verified: no check reached an upstream API.",
              file=sys.stderr)
        return 2
    if failed:
        print(f"\n{len(failed)} check(s) failed.", file=sys.stderr)
        return 1
    print(f"\n{len(passed)} live check(s) passed.")
    return 0


def self_test(config):
    """Prove this checker without credentials: a run that reports PASS must have
    actually read something, and a run that reads nothing must not exit 0."""
    checks = 0

    def expect(condition, label):
        nonlocal checks
        checks += 1
        if not condition:
            print(f"self-test FAIL: {label}", file=sys.stderr)
            return 1
        return 0

    failures = 0
    full = {"GITHUB_TOKEN": "ghp_fakeToken12345", "GITHUB_REPO": "acme/widgets",
            "GITHUB_WORKFLOW_ID": "ci.yml", "VERCEL_TOKEN": "vercel_fakeToken",
            "VERCEL_PROJECT_ID": "prj_1", "DEPLOY_HOOK_URL": "https://hook.test/x",
            "WAHA_OWNER_TOKEN": "owner_fakeToken"}

    class FakeTransport(httpmod.Transport):
        def __init__(self, *answers):
            self.answers = list(answers)

        def request(self, method, url, headers=None, body=None, timeout=15):
            return self.answers.pop(0)

    def resp(status=200, payload=None):
        return httpmod.HttpResponse(status, {}, json.dumps(payload or {}).encode())

    def resolver(host):
        # The self-test must not depend on DNS; see http.assert_public_host.
        return ["93.184.216.34"]

    # 1. Reads pass and say how many rows came back.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(
        resp(200, {"workflow_runs": [{"id": 1, "status": "completed",
                                      "conclusion": "success"}]}),
        resp(200, {"deployments": [{"uid": "d1", "state": "READY"}]})))
    results = run(config=cfgmod.load(full), service=service)
    statuses = {item.operation: item.status for item in results}
    failures += expect(statuses["GET workflow runs"] == PASS, "a good read reports PASS")
    failures += expect(statuses["GET deployments"] == PASS, "a good read reports PASS")
    failures += expect(statuses["workflow dispatch"] == SKIP,
                       "mutations are skipped without the flag")

    # 2. A 401 upstream is FAIL, not PASS -- the whole point of the live layer.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(
        resp(401, {"message": "Bad credentials"}),
        resp(401, {"error": {"message": "no access"}})))
    statuses = {item.operation: item.status
                for item in run(config=cfgmod.load(full), service=service)}
    failures += expect(statuses["GET workflow runs"] == FAIL, "a 401 reports FAIL")
    failures += expect(statuses["GET deployments"] == FAIL, "a 401 reports FAIL")

    # 3. Missing credentials skip, and a run that checked nothing is not a pass.
    empty = cfgmod.load({})
    results = run(config=empty, service=IntegrationService(empty,
                                                           transport=FakeTransport()))
    failures += expect(all(item.status == SKIP for item in results),
                       "no credentials means nothing is claimed")
    failures += expect(not any(item.status == PASS for item in results),
                       "an empty run never reports PASS")

    # 4. The transcript cannot carry a secret.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(
        resp(422, {"message": "rejected ghp_fakeToken12345"}),
        resp(200, {"deployments": []})))
    text = render(run(config=cfgmod.load(full), service=service))
    failures += expect("ghp_fakeToken12345" not in text,
                       "a leaked upstream message is redacted in the report")

    # 5. A half-configured deployment still verifies the half that IS configured.
    # This is the regression that made the checker useless: with no Vercel token it
    # skipped GitHub too, so nothing was verified on any run and nothing failed.
    partial = cfgmod.load({"GITHUB_TOKEN": "ghp_fakeToken12345",
                           "GITHUB_REPO": "acme/widgets",
                           "GITHUB_WORKFLOW_ID": "ci.yml"})
    service = IntegrationService(partial, resolver=resolver, transport=FakeTransport(
        resp(200, {"workflow_runs": [{"id": 1}]})))
    statuses = {item.operation: item.status for item in run(config=partial, service=service)}
    failures += expect(statuses["GET workflow runs"] == PASS,
                       "a configured GitHub is still checked when Vercel is absent")
    failures += expect(statuses["GET deployments"] == SKIP,
                       "an unconfigured Vercel is reported, not silently dropped")

    # 6. --allow-mutations reaches both write endpoints.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(
        resp(200, {"workflow_runs": []}), resp(200, {"deployments": []}),
        resp(204), resp(200, {"id": "dpl_9"})))
    statuses = {item.operation: item.status
                for item in run(config=cfgmod.load(full), service=service,
                                allow_mutations=True)}
    failures += expect(statuses["workflow dispatch"] == PASS, "dispatch reports PASS")
    failures += expect(statuses["deploy hook"] == PASS, "deploy hook reports PASS")

    if failures:
        print(f"integrations live checker self-test: {failures} of {checks} failed",
              file=sys.stderr)
        return 1
    print(f"integrations live checker self-test: ok ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
