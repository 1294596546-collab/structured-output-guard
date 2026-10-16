# -*- coding: utf-8 -*-
"""把 SKILL.md 里六条规则的期望固化成断言。

跑法：
    python tests/test_guard.py

改规则或改坏样本时，这个文件会先挡住你。
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import minimal_guard as guard  # noqa: E402

SAMPLES = json.loads((ROOT / "examples" / "bad_samples.json").read_text(encoding="utf-8"))
BY_ID = {case["id"]: case for case in SAMPLES}
FIELDS = guard.SPEC["fields"]


def run(sample_id):
    return guard.normalize(BY_ID[sample_id]["raw"])


class TestBadSamples(unittest.TestCase):
    """每个坏样本都要在期望的那条规则上被拦下。"""

    def test_every_sample_hits_expected_rules(self):
        problems = []
        for case in SAMPLES:
            _, issues, _ = guard.normalize(case["raw"])
            actual = {i["rule"] for i in issues}
            if not set(case["expected_rules"]) <= actual:
                problems.append("%s 期望 %s，实际 %s"
                                % (case["id"], case["expected_rules"], sorted(actual)))
        self.assertEqual(problems, [], "有样本没在期望的规则上被拦下：" + "；".join(problems))

    def test_rules_are_covered(self):
        """规则 1/2/3/4/6 各要有样本触发；规则 5 是贯穿性的，由下一条测试盯。"""
        triggered = set()
        for case in SAMPLES:
            _, issues, _ = guard.normalize(case["raw"])
            triggered |= {i["rule"] for i in issues}
        self.assertTrue({1, 2, 3, 4, 6} <= triggered,
                        "六条规则没被覆盖全，实际触发：%s" % sorted(triggered))

    def test_rule5_records_every_change_with_kind(self):
        """规则 5：每条改动都要带归档类别，而且两类都要出现过。"""
        kinds = set()
        for case in SAMPLES:
            _, issues, _ = guard.normalize(case["raw"])
            for item in issues:
                self.assertIn(item["kind"], ("format", "business"),
                              "归档类别只能是 format 或 business")
                kinds.add(item["kind"])
        self.assertEqual(kinds, {"format", "business"}, "格式类与业务类都应出现过")


class TestRule1Parse(unittest.TestCase):
    def test_text_wrapped_json_is_extracted(self):
        result, issues, _ = run("R1a-外面裹了话，仍能截出来")
        self.assertEqual(issues, [], "能截出来就不该报问题")
        self.assertEqual(result["category"], "billing")

    def test_non_json_falls_back_to_conservative(self):
        result, issues, needs_human = run("R1b-整段不是 JSON")
        self.assertTrue(needs_human)
        self.assertEqual(result["action"], guard.SPEC["safe_action"], "不可逆动作必须降级")
        self.assertEqual(result["category"], "other")
        self.assertEqual(result["needs_human"], "yes")
        self.assertNotEqual(result["reason"], guard.PLACEHOLDER,
                            "允许留空的字段不许补占位符")


class TestRule2TypeAndRange(unittest.TestCase):
    def test_unit_string_is_coerced(self):
        result, issues, _ = run("R2a-金额是带单位的字符串")
        self.assertEqual(result["amount"], 1234.5)
        self.assertTrue(all(i["kind"] == "format" for i in issues),
                        "只做归一化不该记成业务降级")

    def test_out_of_range_is_blocked(self):
        result, issues, needs_human = run("R2b-金额越界")
        self.assertTrue(needs_human, "越界要人看")
        self.assertTrue(result["amount"] in (None, ""), "越界后该字段应落空")
        self.assertTrue(any(i["rule"] == 2 and i["kind"] == "business" for i in issues))

    def test_case_difference_is_format_only(self):
        result, issues, needs_human = run("R2c-枚举字段大小写差异")
        self.assertEqual(result["priority"], "high")
        self.assertTrue(all(i["kind"] == "format" for i in issues))
        self.assertFalse(needs_human, "只有格式修正时不需要人工")


class TestRule3Enum(unittest.TestCase):
    def test_unknown_enum_goes_conservative(self):
        result, issues, _ = run("R3-枚举外的值")
        self.assertEqual(result["category"], "other")
        self.assertTrue(any(i["rule"] == 3 and i["kind"] == "business" for i in issues))


class TestRule2Reference(unittest.TestCase):
    """第四类字段（引用型）：格式对还不够，得真的查得到。"""

    def test_reference_not_found_is_blocked(self):
        result, issues, needs_human = run("R2d-引用型：格式对但查不到")
        self.assertTrue(needs_human, "查不到的单据号不能放过去")
        self.assertTrue(any(i["rule"] == 2 and i["kind"] == "business"
                            and i["field"] == "order_id" for i in issues))
        self.assertNotEqual(result["order_id"], "9999999999999999")

    def test_reference_pattern_is_checked(self):
        result, issues, needs_human = run("R2e-引用型：格式就不对")
        self.assertTrue(needs_human)
        self.assertTrue(any(i["rule"] == 2 and i["field"] == "order_id" for i in issues))


class TestOptionalRetryBranch(unittest.TestCase):
    """可选分支（追问策略）：钩子由程序提供，不由模型自己决定要不要问。"""

    WRAPPED = "这个工单我处理不了。"
    GOOD = ('{"category":"billing","priority":"normal","amount":234.5,"reason":"重复扣款",'
            '"needs_human":"no","action":"reply_only","order_id":"2026101700012345"}')

    def test_without_hook_no_branch(self):
        """不传钩子时，解析失败直接走全保守值，不该出现"追问"字样。"""
        result, issues, needs_human = guard.normalize(self.WRAPPED)
        self.assertTrue(needs_human)
        self.assertEqual(result["action"], guard.SPEC["safe_action"])
        self.assertFalse([i for i in issues if "追问" in i["detail"]])

    def test_hook_recovers_after_one_retry(self):
        result, issues, needs_human = guard.normalize(
            self.WRAPPED, retry_fn=lambda raw, seen: self.GOOD)
        self.assertFalse(needs_human, "追问成功后不该再要人工")
        self.assertEqual(result["category"], "billing")
        self.assertEqual(result["action"], "reply_only")
        self.assertTrue(any(i["kind"] == "format" and "追问" in i["detail"] for i in issues),
                        "追问成功这件事本身要记一笔")

    def test_hook_falling_twice_ends_conservative(self):
        calls = []

        def always_bad(raw, seen):
            calls.append(raw)
            return "还是没听懂。"

        result, issues, needs_human = guard.normalize(self.WRAPPED, retry_fn=always_bad)
        self.assertEqual(len(calls), 1, "只允许追问一次")
        self.assertTrue(needs_human)
        self.assertEqual(result["action"], guard.SPEC["safe_action"])
        self.assertTrue(any(i["rule"] == 1 and i["kind"] == "business" for i in issues))

    def test_hook_raising_is_treated_as_failure(self):
        def boom(raw, seen):
            raise RuntimeError("网络断了")

        result, issues, needs_human = guard.normalize(self.WRAPPED, retry_fn=boom)
        self.assertTrue(needs_human)
        self.assertEqual(result["action"], guard.SPEC["safe_action"])


class TestRule4EmptyPolicy(unittest.TestCase):
    def test_optional_missing_stays_empty(self):
        result, issues, needs_human = run("R4a-允许留空的字段缺失")
        self.assertEqual(result["reason"], "", "允许留空的字段必须留空")
        self.assertEqual(issues, [])
        self.assertFalse(needs_human)

    def test_required_missing_is_filled_and_flagged(self):
        result, issues, needs_human = run("R4b-必须非空的字段缺失")
        self.assertEqual(result["needs_human"], "yes", "必须非空的字段缺了要落保守值")
        self.assertTrue(any(i["rule"] == 4 for i in issues))
        self.assertTrue(needs_human)


class TestRule6ActionGrade(unittest.TestCase):
    def test_irreversible_action_is_downgraded(self):
        result, issues, needs_human = run("R6-不可逆动作遇上需要人工")
        self.assertEqual(result["action"], guard.SPEC["safe_action"])
        self.assertTrue(any(i["rule"] == 6 for i in issues))
        self.assertTrue(needs_human)

    def test_combo_sample_triggers_several_rules(self):
        result, issues, _ = run("R7-组合：非法枚举 + 缺字段 + 不可逆动作")
        rules = {i["rule"] for i in issues}
        self.assertTrue({3, 4, 6} <= rules, "组合样本应触发 3、4，并由此触发 6")
        self.assertEqual(result["action"], guard.SPEC["safe_action"])
        self.assertEqual(result["category"], "other")


class TestOutputShape(unittest.TestCase):
    """规范化之后结果本身必须合规——这才是这套规则存在的意义。"""

    def test_shape_is_always_valid(self):
        problems = []
        for case in SAMPLES:
            result, _, _ = guard.normalize(case["raw"])
            if set(result) != set(FIELDS):
                problems.append("%s 字段集合不对" % case["id"])
            for name, rule in FIELDS.items():
                value = result.get(name)
                if rule["type"] == "str" and not isinstance(value, str):
                    problems.append("%s.%s 应为字符串，实际 %r" % (case["id"], name, value))
                if rule["type"] == "float" and value not in (None, "") and not isinstance(value, float):
                    problems.append("%s.%s 应为数字或空，实际 %r" % (case["id"], name, value))
                if "enum" in rule and value not in rule["enum"]:
                    problems.append("%s.%s 不在取值集合里：%r" % (case["id"], name, value))
                if rule.get("empty") == "required" and not value:
                    problems.append("%s.%s 必须非空，实际 %r" % (case["id"], name, value))
                if rule.get("empty") == "optional" and value == guard.PLACEHOLDER:
                    problems.append("%s.%s 允许留空，不该补占位符" % (case["id"], name))
        self.assertEqual(problems, [], "输出形状不合规：" + "；".join(problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
