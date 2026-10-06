"""Pure-unit tests for the agent core: no Flask, no network, no database."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent.runtime import extract_json, normalize_plan, parse_action  # noqa: E402
from agent.tools import ToolError, build_registry, _calculator, _clock, _is_public_address, _strip_html  # noqa: E402


class PromptParsingTests(unittest.TestCase):
    def test_plain_json_object(self):
        self.assertEqual(extract_json('{"final":"مرحبا"}'), {"final": "مرحبا"})

    def test_json_inside_code_fence_and_prose(self):
        text = 'بالتأكيد!\n```json\n{"thought":"فكرة","final":"جواب"}\n```\nأتمنى أن يساعدك.'
        self.assertEqual(extract_json(text)["final"], "جواب")

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_json("لا يوجد كائن هنا"))
        self.assertIsNone(extract_json(None))

    def test_nested_braces_are_kept(self):
        raw = extract_json('prefix {"action":{"tool":"clock","args":{}}} suffix')
        self.assertEqual(raw["action"]["tool"], "clock")


class PlanTests(unittest.TestCase):
    def test_plan_is_capped_by_max_steps(self):
        raw = {"steps": [{"title": f"خ{index}", "goal": "هدف"} for index in range(10)]}
        self.assertEqual(len(normalize_plan(raw, "الهدف", 5)), 5)

    def test_missing_plan_degrades_to_single_step(self):
        plan = normalize_plan(None, "علّمني أساسيات بايثون في خمس خطوات", 5)
        self.assertEqual(len(plan), 1)
        self.assertIn("بايثون", plan[0]["goal"])

    def test_string_steps_are_accepted(self):
        plan = normalize_plan({"steps": ["اكتب مقدمة", "اكتب خاتمة"]}, "goal", 5)
        self.assertEqual([item["title"] for item in plan], ["اكتب مقدمة", "اكتب خاتمة"])

    def test_empty_titles_survive(self):
        plan = normalize_plan({"steps": [{"title": "", "goal": "اشرح"}]}, "goal", 5)
        self.assertEqual(plan[0]["title"], "اشرح"[:60])


class ActionTests(unittest.TestCase):
    def test_action_wins_over_final(self):
        action, final = parse_action({"action": {"tool": "clock", "args": None}, "final": "x"})
        self.assertEqual(action, {"tool": "clock", "args": {}})
        self.assertIsNone(final)

    def test_scalar_args_are_wrapped(self):
        action, _ = parse_action({"action": {"tool": "clock", "args": "3"}})
        self.assertEqual(action["args"], {"value": "3"})

    def test_malformed_answer_becomes_final_text(self):
        action, final = parse_action(None, fallback_text="رد نصي عادي")
        self.assertIsNone(action)
        self.assertEqual(final, "رد نصي عادي")

    def test_thought_only_answer_is_used(self):
        action, final = parse_action({"thought": "فكرتي", "final": "  "})
        self.assertIsNone(action)
        self.assertEqual(final, "فكرتي")


class CalculatorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.ctx = object()

    def test_valid_expression(self):
        self.assertEqual(_calculator(self.ctx, {"expression": "round(sqrt(2)*pi, 4)"})["result"], 4.4429)

    def test_import_attempt_blocked(self):
        for payload in ("__import__('os').system('id')", "open('/etc/passwd').read()",
                        "''.__class__", "().__class__.__bases__", "1 if True else 2"):
            with self.assertRaises(ToolError, msg=payload):
                _calculator(self.ctx, {"expression": payload})

    def test_division_by_zero_and_huge_exponent(self):
        with self.assertRaises(ToolError):
            _calculator(self.ctx, {"expression": "1/0"})
        with self.assertRaises(ToolError):
            _calculator(self.ctx, {"expression": "2**99999"})

    def test_unknown_name_blocked(self):
        with self.assertRaises(ToolError):
            _calculator(self.ctx, {"expression": "secret+1"})

    def test_clock_offset_capped(self):
        with self.assertRaises(ToolError):
            _clock(self.ctx, {"offset_days": 999999})
        self.assertIn("iso_utc", _clock(self.ctx, {"offset_days": 1}))


class FetchGuardTests(unittest.TestCase):
    def test_internal_addresses_rejected(self):
        for target in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1",
                       "169.254.169.254", "::1", "0.0.0.0"):
            with self.assertRaises(ToolError, msg=target):
                _is_public_address(target)

    def test_unresolvable_host_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            _is_public_address("no-such-host.invalid")

    def test_html_is_flattened_without_scripts(self):
        html = "<div><script>steal()</script><style>p{}</style><p>مرحبا&nbsp;بالعالم</p></div>"
        text = _strip_html(html)
        self.assertIn("مرحبا بالعالم", text)
        self.assertNotIn("steal", text)
        self.assertNotIn("<p>", text)


class RegistryTests(unittest.TestCase):
    def test_network_tool_requires_opt_in(self):
        registry = build_registry()
        self.assertNotIn("web_fetch", registry.names(type("C", (), {"NETWORK_TOOLS": False})))
        self.assertIn("web_fetch", registry.names(type("C", (), {"NETWORK_TOOLS": True})))

    def test_unknown_tool_rejected(self):
        registry = build_registry()
        with self.assertRaises(ToolError):
            registry.get("run_shell", type("C", (), {"NETWORK_TOOLS": True}))

    def test_disabled_tool_rejected_even_when_known(self):
        registry = build_registry()
        with self.assertRaises(ToolError):
            registry.get("web_fetch", type("C", (), {"NETWORK_TOOLS": False}))

    def test_prompt_lists_only_enabled_tools(self):
        registry = build_registry()
        prompt = registry.prompt_text(type("C", (), {"NETWORK_TOOLS": False}))
        self.assertIn("calculator(", prompt)
        self.assertNotIn("web_fetch(", prompt)
        self.assertIn("needs user approval", registry.prompt_text(type("C", (), {"NETWORK_TOOLS": True})))

    def test_unknown_args_rejected(self):
        registry = build_registry()
        tool = registry.get("calculator", type("C", (), {"NETWORK_TOOLS": False}))
        with self.assertRaises(ToolError):
            tool.validate({"expression": "1+1", "shell": "id"})
        with self.assertRaises(ToolError):
            tool.validate({})


if __name__ == "__main__":
    unittest.main()
