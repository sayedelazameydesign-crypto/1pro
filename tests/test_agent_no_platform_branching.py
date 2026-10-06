"""R6-D: the structural guard -- no host names inside the agent.

A deployment-specific `if` in the core starts as one exception ("just this once, Vercel is
different") and ends as two products that disagree about what a task is. The equivalence
tests in `test_agent_execution.py` prove the semantics match *today*; this file is what
keeps them matching, by refusing the shape that would split them.

Textual scans here run on AST-unparsed source with docstrings stripped, because the prose
in these modules deliberately names the hosts it prevents ("`app.py` is the only place
that knows `VERCEL` exists") -- a raw grep would fail on the safety documentation.
"""
import ast
import inspect
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent import execution  # noqa: E402
AGENT_DIR = ROOT / "backend" / "agent"
APP_PATH = ROOT / "backend" / "app.py"

HOST_TOKENS = ("vercel", "netlify", "render", "promptql", "railway", "fly.io", "lambda")
POLICY_MODULE = "execution.py"


def normalized(source):
    """ast.unparse prefers single quotes; normalising keeps the scans reading like the code."""
    return source.replace("'", '"')


def executable_tree(path):
    """Parse a module and drop docstrings, so a scan sees code and not prose.

    Comments are gone with the tokenizer; docstrings have to go by hand, otherwise every
    sentence that explains a ban reads as a violation of it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    return tree


def code_only(path):
    return ast.unparse(executable_tree(path))


def executable_strings(path):
    return [node.value for node in ast.walk(executable_tree(path))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def string_constants(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def functions_touching(path, predicate):
    """Names of the functions in a module whose code satisfies `predicate`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and predicate(ast.unparse(node)):
            hits.add(node.name)
    return hits


class AgentCoreHasNoHosts(unittest.TestCase):
    def test_no_agent_module_compares_against_a_host(self):
        """Branching is the disease; a label string is just a label.

        `GatewayProvider.label = "promptql"` is data the provider reports. `if mode ==
        "vercel"` in the loop is a fork. So the scan looks at comparisons, not at prose.
        """
        offenders = []
        for path in sorted(AGENT_DIR.glob("*.py")):
            if path.name == POLICY_MODULE:
                continue          # translate the fact here; nowhere else -- see the next test
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Compare):
                    continue
                words = [item.value.lower() for item in ast.walk(node)
                         if isinstance(item, ast.Constant) and isinstance(item.value, str)
                         and any(token in item.value.lower() for token in HOST_TOKENS)]
                hit = [word for word in words if any(token in word for token in HOST_TOKENS)]
                if hit:
                    offenders.append(f"{path.name}:{node.lineno} {hit}")
        self.assertEqual(offenders, [],
                         "the core is comparing against a host name; carry a policy instead")

    def test_even_the_policy_translates_the_host_in_one_function(self):
        # Granting `execution.py` the host vocabulary is not a free pass: it may use it in
        # exactly one place, the resolver. A second comparison would mean the policy layer
        # started negotiating with a host instead of translating it once.
        hit = functions_touching(AGENT_DIR / POLICY_MODULE,
                                 lambda body: any(token in normalized(body) for token in HOST_TOKENS))
        self.assertEqual(hit, {"resolve"}, f"host vocabulary escaped `resolve()`: {sorted(hit)}")

    def test_the_host_vocabulary_lives_in_two_files_only(self):
        allowed = {POLICY_MODULE, "providers.py"}
        for path in sorted(AGENT_DIR.glob("*.py")):
            if path.name in allowed:
                continue
            words = [word.lower() for word in executable_strings(path)]
            for token in HOST_TOKENS:
                self.assertFalse([word for word in words if token in word],
                                 f"{path.name} mentions {token!r}; move the fact to "
                                 f"agent/{POLICY_MODULE} and pass it in as a policy")

    def test_no_agent_module_reads_the_environment(self):
        """Config is the exception, and only for its own knobs -- never for a platform."""
        for path in sorted(AGENT_DIR.glob("*.py")):
            code = code_only(path)
            if path.name == "config.py":
                # Env *names* are literals here (some via `_int("AGENT_...")`), so scan the
                # constants: the AGENT_ namespace is the whole surface an operator may move,
                # and a platform name in it would mean the config layer grew a host branch.
                names = [item for item in string_constants(path) if re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", item)]
                self.assertGreater(len(names), 10, "config.py lost its knob names?")
                for name in names:
                    self.assertTrue(name.startswith("AGENT_"),
                                    f"config.py references {name}: the knob namespace belongs "
                                    f"to the agent, not to the host")
                continue
            self.assertNotIn("environ", code, f"{path.name} reads os.environ")
            self.assertNotIn("getenv", code, f"{path.name} reads os.getenv")

    def test_the_policy_module_is_a_pure_function_of_facts(self):
        code = code_only(AGENT_DIR / POLICY_MODULE)
        self.assertNotIn("environ", code)
        self.assertNotIn("import os", code)
        parameters = list(inspect.signature(execution.resolve).parameters)
        self.assertEqual(parameters, ["ai_mode", "serverless", "config"],
                         "resolve() gains a parameter only with a test that needs it")

    def test_runtime_does_not_even_see_the_mode_as_a_string(self):
        """The loop knows one boolean (request-bound), not a mode vocabulary."""
        runtime = code_only(AGENT_DIR / "runtime.py")
        for literal in ('"inline"', '"queued"', "INLINE", "QUEUED"):
            self.assertNotIn(literal, runtime,
                             f"runtime.py should not speak the mode's name ({literal}); "
                             f"`deps.inline` is the whole interface it was given")
        self.assertIn("self.deps.inline", runtime)

    def test_service_branches_on_mode_only_where_the_schedule_differs(self):
        allowed = {"__init__", "for_request", "start", "submit", "execute_inline",
                   "bootstrap", "describe"}
        found = functions_touching(AGENT_DIR / "service.py", lambda body: "self.mode" in body)
        self.assertTrue(found.issubset(allowed),
                        f"mode logic leaked into {sorted(found - allowed)}; a worker pool and "
                        f"an inline call may differ only in scheduling")


class AppIsTheOnlyHostReader(unittest.TestCase):
    def test_exactly_one_function_reads_vercel(self):
        hit = functions_touching(APP_PATH, lambda body: '"VERCEL"' in normalized(body))
        self.assertEqual(hit, {"execution_policy"},
                         "another route or helper started sniffing the platform")

    def test_the_module_level_host_read_is_a_path_and_nothing_else(self):
        # /tmp-vs-repo is a filesystem fact about the host, and app.py is the platform
        # adapter, so it is allowed to know it -- as long as it stays a path and never a
        # behaviour. A second module-level read would mean the host started deciding logic.
        tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        top_level = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            source = normalized(ast.unparse(node))
            if '"VERCEL"' in source:
                top_level.append(source)
        self.assertEqual(len(top_level), 1, top_level)
        self.assertTrue(top_level[0].startswith("WRITABLE_ROOT ="), top_level[0])

    def test_the_route_reports_the_policy_instead_of_naming_a_mode(self):
        source = APP_PATH.read_text(encoding="utf-8")
        start = source.index("def agent_create_task()")
        body = source[start:start + index_end(source, start)]
        code = re.sub(r'"""[^"]*"""', "", body)
        for literal in ('mode="inline"', 'mode="queued"'):
            self.assertNotIn(literal, code,
                             f"{literal} hard-codes a mode the response should carry from the policy")
        self.assertIn("mode=policy.mode", code)
        self.assertIn("execution=policy.describe()",
                      source[source.index("def agent_config()"):])

    def test_one_construction_site_for_the_agents_wiring(self):
        """Two ways to build Deps is how modes start to differ in more than their schedule."""
        code = normalized(code_only(APP_PATH))
        self.assertEqual(code.count("def agent_deps"), 1)
        self.assertEqual(code.count("AgentDeps(**"), 2,
                         "the boot service and the request service, both from agent_deps()")
        self.assertEqual(code.count('"record_cooldown"'), 1,
                         "a second copy of the hook wiring means a fork in the loop's behaviour")
        self.assertNotIn("class _InlineLimits", code,
                         "caps live in agent/execution.py, where they can be tested")


class BudgetFitsThePlatform(unittest.TestCase):  # noqa: E301
    """The numbers are a contract with the host, so the pair is checked together."""

    def setUp(self):
        self.execution = execution

    def test_serverless_budget_fits_vercels_declared_ceiling(self):
        declared = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        duration = declared["functions"]["api/index.py"]["maxDuration"]
        self.assertLess(self.execution.SERVERLESS_CAPS["DEADLINE_SECONDS"], duration,
                        "the task must reach a terminal state before the function is frozen")
        self.assertLess(self.execution.SERVERLESS_CAPS["PROVIDER_TIMEOUT_SECONDS"],
                        self.execution.SERVERLESS_CAPS["DEADLINE_SECONDS"],
                        "one slow generation must not eat the whole window")

    def test_inline_caps_are_below_every_public_default(self):
        from agent.config import AgentConfig
        for name, cap in sorted(dict(self.execution.INLINE_CAPS,
                                     **self.execution.SERVERLESS_CAPS).items()):
            self.assertLess(cap, getattr(AgentConfig, name),
                            f"{name}: a cap above the default silently does nothing, which is "
                            f"how a budget stops being enforced without anyone noticing")

    def test_approvals_are_only_a_queued_property(self):
        module = self.execution
        queued = module.ExecutionPolicy(module.QUEUED, config=None)
        inline = module.ExecutionPolicy(module.INLINE, config=None)
        self.assertTrue(queued.allows_approvals)
        self.assertFalse(inline.allows_approvals)
        self.assertTrue(module.ExecutionPolicy(module.INLINE, config=None)
                        .uses_request_visitor_token)


def index_end(source, start):
    """End of a top-level function body: the next top-level `def ` or decorator."""
    match = re.search(r"\n(?=@|def |class )", source[start + 1:])
    return (match.start() + 1) if match else len(source) - start - 1


if __name__ == "__main__":
    unittest.main()
