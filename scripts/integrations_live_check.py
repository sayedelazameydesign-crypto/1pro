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

    def line(self, width=26, provider_width=13):
        return f"{self.provider:<{provider_width}} {self.operation:<{width}} {self.status}" \
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


def check_render_read(service, redact):
    """Reads only; changes nothing upstream."""
    try:
        deps = service.render_deploys()
        return Result("Render", "GET deploys", PASS, f"{deps['count']} deploy(s)")
    except httpmod.IntegrationError as error:
        return _failure("Render", "GET deploys", error, redact)


def check_drive_read(service, redact):
    """Reads only. A Drive listing is the cheapest proof that the OAuth grant is
    still alive, and it is the only one of the four reads that a revoked *account*
    (not just a revoked key) also breaks."""
    try:
        files = service.drive_files()
        return Result("Google Drive", "GET folder", PASS,
                      "{} file(s)".format(files["count"])
                      + (f" in {files['folder_id']}" if files["folder_id"] else " in My Drive"))
    except httpmod.IntegrationError as error:
        return _failure("Google Drive", "GET folder", error, redact)


def check_reads(service, redact):
    """Every read the portal offers, decided provider by provider.

    Kept as one call for the live test layer. A service built without Render or
    Drive is not asked to check them: ``service.config`` is the same object the
    routes will use, so what this reports and what the page can do cannot drift.
    """
    cfg = service.config
    out = [check_github_read(service, redact), check_vercel_read(service, redact)]
    if cfg.render.configured:
        out.append(check_render_read(service, redact))
    if cfg.drive.configured:
        out.append(check_drive_read(service, redact))
    return out


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


def _render_hook(service, redact):
    try:
        hook = service.render_deploy()
        return Result("Render", "deploy hook", PASS, f"status={hook.get('status')}")
    except httpmod.IntegrationError as error:
        return _failure("Render", "deploy hook", error, redact)


def _drive_probe(service, redact):
    """Creates one small file, and says so -- the only check here that writes to a
    place that is not a deployment. The name carries the UTC date so a stray probe
    is self-explaining in the owner's Drive."""
    name = f"waha-live-check-{time.strftime('%Y%m%d', time.gmtime())}.md"
    try:
        created = service.drive_upload(name, "Live-check probe file. Safe to delete.\n")
        return Result("Google Drive", "create probe file", PASS,
                      f"id={created['file']['id']}")
    except httpmod.IntegrationError as error:
        return _failure("Google Drive", "create probe file", error, redact)


def check_mutations(service, redact, ref="main"):
    """The write operations. Only called with --allow-mutations."""
    return [_dispatch(service, redact, ref), _hook(service, redact),
            _render_hook(service, redact), _drive_probe(service, redact)]


def skip_mutations(reason="needs --allow-mutations"):
    return [Result("GitHub", "workflow dispatch", SKIP, reason),
            Result("Vercel", "deploy hook", SKIP, reason),
            Result("Render", "deploy hook", SKIP, reason),
            Result("Google Drive", "create probe file", SKIP, reason)]


def unconfigured_checks(config):
    """Report what cannot be checked at all, instead of silently passing.

    A run that checks nothing and exits 0 is worse than a failure: it is the one
    outcome that reads as "verified" while proving nothing.

    Rows are keyed by (provider, operation) by ``run``, and that tuple exists
    because two providers now have an operation called ``deploy hook``: keying by
    the operation name alone would let Vercel's row answer for Render's.
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
    # The hook rows key on the hook, not on the read credential: a deployment with a
    # Vercel read token and no hook is not a credential failure waiting to be
    # reported as one, and --allow-mutations must not turn it into a FAIL.
    if not config.vercel.hook_configured:
        out.append(Result("Vercel", "deploy hook", SKIP, "missing DEPLOY_HOOK_URL"))
    if not config.render.configured:
        out.append(Result("Render", "GET deploys", SKIP,
                          "missing " + ", ".join(
                              name for name in config.render.missing()
                              if name != "RENDER_DEPLOY_HOOK_URL")))
    if not config.render.hook_configured:
        out.append(Result("Render", "deploy hook", SKIP,
                          "missing RENDER_DEPLOY_HOOK_URL"))
    if not config.drive.configured:
        out.append(Result("Google Drive", "GET folder", SKIP,
                          "missing " + ", ".join(config.drive.missing())))
        out.append(Result("Google Drive", "create probe file", SKIP,
                          "missing " + ", ".join(config.drive.missing())))
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
    skipped = {(item.provider, item.operation): item
               for item in unconfigured_checks(config)}

    def read_or_skip(provider, operation, check):
        key = (provider, operation)
        return skipped[key] if key in skipped else check()

    results = [read_or_skip("GitHub", "GET workflow runs",
                            lambda: check_github_read(service, redact)),
               read_or_skip("Vercel", "GET deployments",
                            lambda: check_vercel_read(service, redact)),
               read_or_skip("Render", "GET deploys",
                            lambda: check_render_read(service, redact)),
               read_or_skip("Google Drive", "GET folder",
                            lambda: check_drive_read(service, redact))]
    if not allow_mutations:
        results.extend(skip_mutations())
        return results

    def write_or_skip(provider, operation, write):
        key = (provider, operation)
        results.append(skipped[key] if key in skipped else write())

    write_or_skip("GitHub", "workflow dispatch", lambda: _dispatch(service, redact, ref))
    write_or_skip("Vercel", "deploy hook", lambda: _hook(service, redact))
    write_or_skip("Render", "deploy hook", lambda: _render_hook(service, redact))
    write_or_skip("Google Drive", "create probe file", lambda: _drive_probe(service, redact))
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
        # stderr, not stdout: with --json the whole of stdout is a payload, and a
        # note in front of it is a parse error for every consumer of that flag.
        print("WAHA_OWNER_TOKEN is not set; the /integrations page would refuse "
              "every call even with working provider tokens.", file=sys.stderr)
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
    statuses = {(item.provider, item.operation): item.status for item in results}
    failures += expect(statuses[("GitHub", "GET workflow runs")] == PASS,
                       "a good read reports PASS")
    failures += expect(statuses[("Vercel", "GET deployments")] == PASS,
                       "a good read reports PASS")
    failures += expect(statuses[("GitHub", "workflow dispatch")] == SKIP,
                       "mutations are skipped without the flag")
    failures += expect(statuses[("Google Drive", "create probe file")] == SKIP,
                       "a Drive write is skipped without the flag too")
    failures += expect(statuses[("Render", "GET deploys")] == SKIP,
                       "an unconfigured provider is reported, not dropped")

    # 2. A 401 upstream is FAIL, not PASS -- the whole point of the live layer.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(
        resp(401, {"message": "Bad credentials"}),
        resp(401, {"error": {"message": "no access"}})))
    statuses = {(item.provider, item.operation): item.status
                for item in run(config=cfgmod.load(full), service=service)}
    failures += expect(statuses[("GitHub", "GET workflow runs")] == FAIL,
                       "a 401 reports FAIL")
    failures += expect(statuses[("Vercel", "GET deployments")] == FAIL, "a 401 reports FAIL")

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
    statuses = {(item.provider, item.operation): item.status
                for item in run(config=partial, service=service)}
    failures += expect(statuses[("GitHub", "GET workflow runs")] == PASS,
                       "a configured GitHub is still checked when Vercel is absent")
    failures += expect(statuses[("Vercel", "GET deployments")] == SKIP,
                       "an unconfigured Vercel is reported, not silently dropped")

    # 6. --allow-mutations reaches both write endpoints.
    service = IntegrationService(cfgmod.load(full), resolver=resolver,
                                 transport=FakeTransport(
        resp(200, {"workflow_runs": []}), resp(200, {"deployments": []}),
        resp(204), resp(200, {"id": "dpl_9"})))
    statuses = {(item.provider, item.operation): item.status
                for item in run(config=cfgmod.load(full), service=service,
                                allow_mutations=True)}
    failures += expect(statuses[("GitHub", "workflow dispatch")] == PASS,
                       "dispatch reports PASS")
    failures += expect(statuses[("Vercel", "deploy hook")] == PASS, "deploy hook reports PASS")
    # The two hooks are independent rows even though they share a name: Vercel's
    # answered, Render's was never configured. Collapsing them by operation name is
    # how one provider's PASS would have signed for the other's.
    failures += expect(statuses[("Render", "deploy hook")] == SKIP,
                       "a second provider's hook never borrows the first provider's verdict")

    # 7. A connection that dies before any response is BLOCKED, not FAIL. No token
    # was ever sent, and FAIL would accuse a working credential for the network's
    # behaviour. The code names the observable fact only -- the socket cannot say
    # whether a middlebox, a firewall or the upstream itself ended the connection.
    egress = httpmod.IntegrationError("TLS/SSL connection has been closed (EOF)",
                                      code="pre_http_network_failure")
    results = run(config=cfgmod.load(full),
                  service=IntegrationService(cfgmod.load(full), resolver=resolver,
                                             transport=FakeTransport(egress, egress)))
    statuses = {(item.provider, item.operation): item.status for item in results}
    failures += expect(statuses[("GitHub", "GET workflow runs")] == BLOCKED,
                       "a pre-HTTP connection failure is BLOCKED, not FAIL")
    failures += expect(statuses[("Vercel", "GET deployments")] == BLOCKED,
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
    statuses = {(item.provider, item.operation): item.status
                for item in run(config=cfgmod.load(full), service=service,
                                allow_mutations=True)}
    failures += expect(statuses[("GitHub", "workflow dispatch")] == BLOCKED,
                       "a blocked dispatch never claims a CI run started")
    failures += expect(statuses[("Vercel", "deploy hook")] == BLOCKED,
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

    # 12. The two providers the portal gained are actually checked. The reason this
    # case exists at all: the checker shipped with GitHub and Vercel hardcoded, so a
    # deployment could add two integrations and still be told "verified" by a run
    # that never looked at them.
    four = dict(full, RENDER_API_KEY="rnd_fakeKey1234567890",
                RENDER_SERVICE_ID="srv-fake",
                RENDER_DEPLOY_HOOK_URL="https://api.render.com/deploy/srv-fake?key=fakehookkey",
                GOOGLE_DRIVE_ACCESS_TOKEN="ya29.fakeAccessTokenValueForTests")
    four_cfg = cfgmod.load(four)
    service = IntegrationService(four_cfg, resolver=resolver, transport=FakeTransport(
        resp(200, {"workflow_runs": []}), resp(200, {"deployments": []}),
        resp(200, [{"deploy": {"id": "dep-1", "status": "live",
                               "commit": {"ref": "main", "id": "c" * 40, "message": "x"}}}]),
        resp(200, {"files": [{"id": "f1", "name": "a.md", "size": "1"}]})))
    results = run(config=four_cfg, service=service)
    text = render(results)
    statuses = {(item.provider, item.operation): item.status for item in results}
    failures += expect(statuses[("Render", "GET deploys")] == PASS,
                       "a Render that answered is reported as read")
    failures += expect(statuses[("Google Drive", "GET folder")] == PASS,
                       "a Drive that answered is reported as read")
    failures += expect("1 deploy(s)" in text and "1 file(s)" in text,
                       "the row counts a read returned never reach the report")
    failures += expect("fakehookkey" not in text and
                       "ya29.fakeAccessTokenValueForTests" not in text,
                       "the bearer and the hook key never reach the transcript")
    failures += expect(exit_code(results) == 0, "four reads passing is a 0")

    # 13. An answered 401 from a new provider is a verdict; a dead socket is not.
    # Drive is the case worth pinning: its failure mode is a grant Google revoked,
    # and the fix for that is the opposite of the fix for a blocked port.
    service = IntegrationService(four_cfg, resolver=resolver, transport=FakeTransport(
        resp(200, {"workflow_runs": []}), resp(200, {"deployments": []}),
        resp(401, {"message": "Bad API key"}),
        httpmod.IntegrationError("TLS/SSL connection has been closed (EOF)",
                                 code="pre_http_network_failure")))
    statuses = {(item.provider, item.operation): item.status
                for item in run(config=four_cfg, service=service)}
    failures += expect(statuses[("Render", "GET deploys")] == FAIL,
                       "a 401 from Render accuses the key and says so")
    failures += expect(statuses[("Google Drive", "GET folder")] == BLOCKED,
                       "a Drive connection that died before a response is BLOCKED")

    # 14. A Drive write is a write: it waits for --allow-mutations even when the
    # grant is live, because it leaves a file in the owner's Drive.
    service = IntegrationService(four_cfg, resolver=resolver, transport=FakeTransport(
        resp(200, {"workflow_runs": []}), resp(200, {"deployments": []}),
        resp(200, []), resp(200, {"files": []})))
    statuses = {(item.provider, item.operation): item.status
                for item in run(config=four_cfg, service=service)}
    failures += expect(statuses[("Google Drive", "create probe file")] == SKIP,
                       "a probe file is never created by a read-only run")

    if failures:
        print(f"integrations live checker self-test: {failures} of {checks} failed",
              file=sys.stderr)
        return 1
    print(f"integrations live checker self-test: ok ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
