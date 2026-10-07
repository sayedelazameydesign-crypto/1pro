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

A failure is graded by *when* it happened, not by how alarming it looks. An HTTP
401 is a credential verdict and reports FAIL. A connection that dies before any
response exists -- a TLS handshake torn down, a refused port, a DNS failure, a
timeout -- never asked the credential anything, so it reports BLOCKED rather than
failing a working token for the network's behaviour. Conflating the two is how a
transport failure becomes a false accusation against a healthy credential, and the
two have opposite fixes.

Note what is *not* claimed. BLOCKED deliberately names no cause: a middlebox, a
firewall, a closed port and an upstream that walked away are indistinguishable
from this side of the socket, so the checker states only what it can prove -- no
HTTP response ever existed, so the credential was never judged.

Exit codes: 0 every attempted check passed; 1 a credential or permission check
failed; 2 no check was attempted at all, so nothing was verified; 3 nothing
failed, but at least one check was blocked before it could ask. 1 outranks 3,
which outranks 2, and that order is the contract: a proven credential failure is
never masked by an unverified one, and a check that was attempted and blocked is
not the same thing as a check nobody asked for.

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

PASS, FAIL, SKIP, BLOCKED = "PASS", "FAIL", "SKIP", "BLOCKED"


def network_blocked(error):
    """True when the call failed *before* any HTTP response existed.

    The set lives in ``integrations.http`` next to the transport that raises the
    codes, so a new network failure cannot be added without this checker seeing
    it. Read that name as: the credential was never judged.
    """
    return getattr(error, "code", "") in httpmod.NETWORK_FAILURE_CODES


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


def _failure(provider, operation, error, redact):
    """A failed call, graded by whether any HTTP response existed.

    FAIL is reserved for an upstream that actually answered: a 401, a 403 on the
    wrong team, a 404 for a repo the token cannot see. BLOCKED means the request
    died on the way out, so the token is neither cleared nor accused.
    """
    detail = f"{error.code}: {redact(error.message)}"
    status = BLOCKED if network_blocked(error) else FAIL
    return Result(provider, operation, status, detail)


def check_github_read(service, redact):
    """Reads only; changes nothing upstream."""
    try:
        runs = service.github_runs()
        return Result("GitHub", "GET workflow runs", PASS, f"{runs['count']} run(s)")
    except httpmod.IntegrationError as error:
        return _failure("GitHub", "GET workflow runs", error, redact)


def check_vercel_read(service, redact):
    """Reads only; changes nothing upstream."""
    try:
        deps = service.vercel_deployments()
        return Result("Vercel", "GET deployments", PASS,
                      f"{deps['count']} deployment(s)")
    except httpmod.IntegrationError as error:
        return _failure("Vercel", "GET deployments", error, redact)


def check_reads(service, redact):
    """Both reads. Kept as one call for the live test layer."""
    return [check_github_read(service, redact), check_vercel_read(service, redact)]


def _dispatch(service, redact, ref):
    try:
        dispatched = service.github_dispatch(ref=ref)
        return Result("GitHub", "workflow dispatch", PASS, f"ref={dispatched['ref']}")
    except httpmod.IntegrationError as error:
        return _failure("GitHub", "workflow dispatch", error, redact)


def _hook(service, redact):
    try:
        hook = service.vercel_deploy()
        return Result("Vercel", "deploy hook", PASS,
                      f"id={hook.get('deployment_id')}")
    except httpmod.IntegrationError as error:
        # Worth being precise here: BLOCKED on this operation means no deployment
        # was triggered, and a PASS must never be claimed for one that never left
        # the machine.
        return _failure("Vercel", "deploy hook", error, redact)


def check_mutations(service, redact, ref="main"):
    """The two write operations. Only called with --allow-mutations."""
    return [_dispatch(service, redact, ref), _hook(service, redact)]


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


def exit_code(results):
    """The verdict of a run, in one place and testable without a network.

    Precedence is the contract here, not a set of independent numbers: a proven
    credential failure outranks an unverified check, and a check that was
    attempted and blocked outranks the empty run that never asked. Getting the
    order wrong is invisible in a passing run and wrong exactly when it matters --
    an all-blocked run used to report 2, which reads as "nothing was configured"
    while a credential sat unverified.
    """
    if any(item.status == FAIL for item in results):
        return 1
    if any(item.status == BLOCKED for item in results):
        return 3
    if not any(item.status == PASS for item in results):
        return 2
    return 0


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
    results.append(_dispatch(service, redact, ref))
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
    blocked = [item for item in results if item.status == BLOCKED]
    passed = [item for item in results if item.status == PASS]
    code = exit_code(results)
    if code == 2:
        print("\nnothing was verified: no check was attempted -- every operation "
              "was skipped. Configure the credentials (or pass --allow-mutations "
              "for the write checks) and re-run.", file=sys.stderr)
    elif code == 1:
        print(f"\n{len(failed)} check(s) failed.", file=sys.stderr)
        if blocked:
            print(f"{len(blocked)} further check(s) were BLOCKED before any HTTP "
                  "response and stay unverified.", file=sys.stderr)
    elif code == 3:
        # Deliberately not exit 0: some of the asked-for work is unverified. And
        # deliberately not exit 1: nothing here says a credential is bad.
        print(f"\n{len(blocked)} check(s) BLOCKED before any HTTP response, so the "
              "credential was never judged -- this is not a token verdict.\n"
              "Re-run where the vendor API is reachable; a runner with unrestricted "
              "network access can, and this repo already passes both variables there.",
              file=sys.stderr)
    else:
        print(f"\n{len(passed)} live check(s) passed.")
    return code


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
            "VERCEL_PROJECT_ID": "prj_1",
            "DEPLOY_HOOK_URL": "https://api.vercel.com/v1/integrations/deploy/prj_1/fake-hook",
            "WAHA_OWNER_TOKEN": "owner_fakeToken"}

    class FakeTransport(httpmod.Transport):
        """Answers in order; an ``Exception`` in the list is raised instead."""

        def __init__(self, *answers):
            self.answers = list(answers)

        def request(self, method, url, headers=None, body=None, timeout=15,
                    resolved_addresses=None):
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

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

    # 7. A connection that dies before any response is BLOCKED, not FAIL. No token
    # was ever sent, and FAIL would accuse a working credential for the network's
    # behaviour. The code names the observable fact only -- the socket cannot say
    # whether a middlebox, a firewall or the upstream itself ended the connection.
    egress = httpmod.IntegrationError("TLS/SSL connection has been closed (EOF)",
                                      code="pre_http_network_failure")
    results = run(config=cfgmod.load(full),
                  service=IntegrationService(cfgmod.load(full), resolver=resolver,
                                             transport=FakeTransport(egress, egress)))
    statuses = {item.operation: item.status for item in results}
    failures += expect(statuses["GET workflow runs"] == BLOCKED,
                       "a pre-HTTP connection failure is BLOCKED, not FAIL")
    failures += expect(statuses["GET deployments"] == BLOCKED,
                       "a pre-HTTP connection failure is BLOCKED, not FAIL")
    failures += expect("pre_http_network_failure" in render(results),
                       "the report names when it failed, so it is not read as a bad token")
    failures += expect(not any(item.status == PASS for item in results),
                       "a blocked run claims nothing")

    # 8. Every code in the set means "no response ever existed". If the transport grows
    # one and this checker does not, a token gets blamed for a network policy.
    for code in sorted(httpmod.NETWORK_FAILURE_CODES):
        failures += expect(network_blocked(httpmod.IntegrationError("no answer", code=code)),
                           f"{code} carries no verdict about the credential")

    # 9. The inverse matters as much: an answered 401 *is* a credential verdict and
    # must not be softened into BLOCKED.
    failures += expect(not network_blocked(
        httpmod.IntegrationError("Bad credentials", code="ghp_unauthorized")),
        "a 401 stays a FAIL")

    # 10. Under --allow-mutations a blocked call must not report PASS either: on the
    # deploy hook, PASS is the report's way of saying a real deployment was triggered.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(resp(200, {"workflow_runs": []}),
                                                         resp(200, {"deployments": []}),
                                                         egress, egress))
    statuses = {item.operation: item.status
                for item in run(config=cfgmod.load(full), service=service,
                                allow_mutations=True)}
    failures += expect(statuses["workflow dispatch"] == BLOCKED,
                       "a blocked dispatch never claims a CI run started")
    failures += expect(statuses["deploy hook"] == BLOCKED,
                       "a blocked deploy hook never claims a deployment was triggered")

    # 11. The exit-code contract, pinned as precedence rather than as separate
    # numbers. The all-blocked case is the one that was wrong: it returned 2, which
    # reads as "nothing was configured" while a credential sat unverified.
    def verdict(*statuses):
        return exit_code([Result("GitHub", "GET workflow runs", status)
                          for status in statuses])

    failures += expect(verdict(PASS) == 0, "a passed run exits 0")
    failures += expect(verdict(PASS, SKIP) == 0, "verifying half still exits 0")
    failures += expect(verdict(FAIL) == 1, "a failed run exits 1")
    failures += expect(verdict(FAIL, BLOCKED) == 1,
                       "a proven credential failure outranks an unverified one")
    failures += expect(verdict(BLOCKED) == 3, "an all-blocked run exits 3, not 2")
    failures += expect(verdict(BLOCKED, SKIP) == 3, "one blocked check is enough for 3")
    failures += expect(verdict(PASS, BLOCKED) == 3,
                       "a partial pass does not hide an unverified check")
    failures += expect(verdict(SKIP) == 2, "a run that attempted nothing exits 2")
    failures += expect(verdict(SKIP, SKIP) == 2, "skips alone are still 2")

    if failures:
        print(f"integrations live checker self-test: {failures} of {checks} failed",
              file=sys.stderr)
        return 1
    print(f"integrations live checker self-test: ok ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
