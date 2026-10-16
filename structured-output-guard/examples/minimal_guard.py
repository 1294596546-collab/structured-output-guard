# -*- coding: utf-8 -*-
"""结构化输出兜底的最小可跑示例。

跑法：
    python examples/minimal_guard.py

它做三件事：
  1. 用一份**中性**的字段规格（下面 SPEC）演示六条规则怎么落地；
  2. 逐条跑坏样本（bad_samples.json），打印「样本 → 期望规则 → 实际触发 → 是否需要人工」；
  3. 统计六条规则各自被触发了多少次；有样本不符合预期就退出码非 0。

这是最小演示，不是可直接上生产的库：不做重试、不做限流、不做脱敏、不做审计留存。
规则原文见 ../SKILL.md，怎么套到你自己的项目见 ../README.md。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
log = logging.getLogger("structured-output-guard")

# 必须非空、但模型没给时补的占位符——注意：只在"必须非空"的字段上补，允许留空的不补。
PLACEHOLDER = "(缺失，待人工补充)"

# 引用型字段要核对的东西。演示用一张写死的表；真实项目里换成你的数据库或接口。
REFERENCE_TABLE = {"2026101700012345", "2026101800043210"}

# 中性的字段规格：字段名与取值集合都只是示例，替换成你自己的就行。
SPEC = {
    "fields": {
        "category": {"type": "str", "enum": ["billing", "shipping", "account", "other"],
                     "empty": "required", "conservative": "other"},
        "priority": {"type": "str", "enum": ["low", "normal", "high"],
                     "empty": "required", "conservative": "high"},
        "amount": {"type": "float", "min": 0, "max": 200000,
                   "empty": "optional", "on_violation": "empty"},
        "reason": {"type": "str", "empty": "optional"},
        "needs_human": {"type": "str", "enum": ["yes", "no"],
                        "empty": "required", "conservative": "yes"},
        "action": {"type": "str", "enum": ["reply_only", "transfer", "refund_irreversible"],
                   "empty": "required", "conservative": "reply_only"},
        # 引用型：格式对了还得能查到，查不到一样不合法
        "order_id": {"type": "str", "empty": "required",
                     "reference": {"pattern": r"^\d{16}$", "hint": "必须是 16 位数字且在系统里查得到"}},
    },
    # 动作分级：这三个动作里哪个收不回来
    "irreversible_actions": ["refund_irreversible"],
    "safe_action": "reply_only",
}


def _issue(rule, kind, field, detail):
    """rule: 1~6；kind: format（无害）或 business（要人看）。"""
    return {"rule": rule, "kind": kind, "field": field, "detail": detail}


def _number(text):
    """把 '1,234.5 元' 这种数字串里的噪声去掉；去掉后不是数字就返回 None。"""
    cleaned = re.sub(r"[^\d.\-]", "", str(text))
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _extract(raw):
    """规则 1：解析与截取。返回 (dict 或 None, issues)。"""
    if isinstance(raw, dict):
        return raw, []
    text = raw if isinstance(raw, str) else ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, [_issue(1, "business", "(整段)", "输出里找不到 JSON 对象")]
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None, [_issue(1, "business", "(整段)", "截出来的片段不是合法 JSON")]
    if not isinstance(data, dict):
        return None, [_issue(1, "business", "(整段)", "截出来的不是 JSON 对象")]
    return data, []


def _fallback(rule):
    """这个字段"兜底时该填什么"。"""
    if "conservative" in rule:
        return rule["conservative"]
    if rule.get("empty") == "optional":
        return "" if rule["type"] == "str" else None
    return PLACEHOLDER


def _coerce(value, rule):
    """规则 2：类型与范围。返回 (是否通过, 归一后的值, 格式类说明或 None)。"""
    if rule["type"] == "float":
        if isinstance(value, bool):
            return False, None, "布尔值不能当数字用"
        if isinstance(value, (int, float)):
            number, note = float(value), None
        else:
            number, note = _number(value), "从字符串归一成数字"
            if number is None:
                return False, None, "数字字段给的不是数字"
        low, high = rule.get("min"), rule.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            return False, None, "数值越界（%s ~ %s）" % (low, high)
        return True, number, note
    # 字符串
    if isinstance(value, (list, tuple)):
        return True, "；".join(str(v).strip() for v in value if str(v).strip()), "数组拼成字符串"
    if isinstance(value, dict):
        return False, None, "对象不能当字符串用"
    text = str(value).strip()
    if not text:
        return False, None, "空字符串"
    if "enum" in rule:  # 只有大小写/空格差异的，算格式修正，替模型改成规范写法
        for member in rule["enum"]:
            if text.lower() == str(member).lower():
                return True, member, None if text == member else "枚举值大小写归一"
    return True, text, None


def _check_reference(value, rule):
    """规则 2（有效性）：引用型字段要格式对、而且真的查得到。返回 (是否通过, 说明)。"""
    ref = rule.get("reference")
    if not ref:
        return True, None
    pattern = ref.get("pattern")
    if pattern and not re.match(pattern, str(value)):
        return False, "格式不符（%s）" % ref.get("hint", pattern)
    if str(value) not in REFERENCE_TABLE:
        return False, "在系统里查不到这条记录"
    return True, None


def normalize(raw, spec=SPEC, retry_fn=None, max_retries=1):
    """把一段模型输出规范成合规结果。

    返回 (result, issues, needs_human)。规则顺序固定：
    解析 → 类型与范围 → 枚举收敛 → 留空判断 → 记录 → 动作分级。

    retry_fn 是**可选分支**（追问策略）的钩子：
        传 None（默认）→ 解析失败直接落全保守值；
        传入一个函数 f(raw, issues) -> str → 解析失败时先追问一次，拿它的新输出重解析；
        追问仍然失败 → 落全保守值 + 转人工，并写一条 ERROR 级日志。
    追问由这个钩子触发，不由模型自己决定要不要问。
    """
    issues = []
    data, parse_issues = _extract(raw)
    issues.extend(parse_issues)
    fields = spec["fields"]

    # 规则 1 的可选分支：追问一次
    tries = 0
    while data is None and retry_fn is not None and tries < max_retries:
        tries += 1
        log.error("解析失败，按追问分支发起第 %d 次重问", tries)
        try:
            raw = retry_fn(raw, issues)
        except Exception as exc:  # noqa: BLE001 —— 追问本身失败也算追问失败
            log.error("追问调用失败：%s", exc)
            break
        data, more = _extract(raw)
        if data is not None:
            # 追问成功了：把"首次解析失败"从业务级降成格式级——它已经被修好了，
            # 不能因为一次"已经恢复的失败"就要求人工。
            issues = [i for i in issues if not (i["rule"] == 1 and i["kind"] == "business")]
            issues.append(_issue(1, "format", "(整段)",
                                 "首次解析失败，追问 %d 次后成功（已恢复，不需人工）" % tries))
            break
        issues.extend(more)

    if data is None:  # 规则 1 失败路径：不可恢复 → 全部落保守值
        log.error("解析失败且追问无果，落全保守值 + 转人工")
        result = {name: _fallback(rule) for name, rule in fields.items()}
        detail = ("解析失败，全部落保守值" if tries == 0
                  else "追问 %d 次仍失败，全部落保守值 + 转人工" % tries)
        issues.append(_issue(1, "business", "(整段)", detail))
        return result, issues, True

    result = {}
    for name, rule in fields.items():
        value = data.get(name, None)
        missing = value is None or (isinstance(value, str) and not value.strip())
        if missing:
            if rule.get("empty") == "optional":
                result[name] = _fallback(rule)          # 规则 4：允许留空就留空，不记问题
                continue
            filled = _fallback(rule)                     # 规则 4：必须非空 → 补占位/保守值
            result[name] = filled
            issues.append(_issue(4, "business", name, "必须非空却缺失，补 %r" % (filled,)))
            continue

        passed, coerced, note = _coerce(value, rule)      # 规则 2
        if not passed:
            filled = _fallback(rule)
            result[name] = filled
            issues.append(_issue(2, "business", name, "%s，落到 %r" % (note, filled)))
            continue
        if note:
            issues.append(_issue(2, "format", name, note))
        result[name] = coerced

        ok, why = _check_reference(coerced, rule)          # 规则 2：引用有效性
        if not ok:
            filled = _fallback(rule)
            result[name] = filled
            issues.append(_issue(2, "business", name, "%s，落到 %r" % (why, filled)))
            continue

        if "enum" in rule and coerced not in rule["enum"]:  # 规则 3
            conservative = rule["conservative"]
            result[name] = conservative
            issues.append(_issue(3, "business", name,
                                 "枚举外的值 %r，落到保守值 %r" % (coerced, conservative)))

    # 规则 6：动作分级。只要出现业务降级、或已经需要人工，不可逆动作就降级。
    if result.get("needs_human") == "yes" or any(i["kind"] == "business" for i in issues):
        if result.get("action") in spec["irreversible_actions"]:
            old = result["action"]
            result["action"] = spec["safe_action"]
            result["needs_human"] = "yes"
            issues.append(_issue(6, "business", "action",
                                 "不可逆动作 %r 降级为 %r" % (old, result["action"])))

    needs_human = result.get("needs_human") == "yes" or any(
        i["kind"] == "business" for i in issues)
    return result, issues, needs_human


def _rules_of(issues):
    return sorted({i["rule"] for i in issues})


def main():
    logging.basicConfig(level=logging.ERROR, format="  [ERROR] %(message)s")
    samples = json.loads((HERE / "bad_samples.json").read_text(encoding="utf-8"))
    print("%-34s %-12s %-12s %-6s %s" % ("样本", "期望规则", "实际触发", "人工", "结果"))
    print("-" * 96)
    counter = {n: 0 for n in range(1, 7)}
    failed = []
    for case in samples:
        result, issues, needs_human = normalize(case["raw"])
        actual = _rules_of(issues)
        for n in actual:
            counter[n] += 1
        expected = case["expected_rules"]
        ok = set(expected) <= set(actual)
        if not ok:
            failed.append(case["id"])
        print("%-34s %-12s %-12s %-6s %s" % (
            case["id"], expected or "无", actual or "无",
            "是" if needs_human else "否",
            "OK" if ok else "不符合预期"))
        for item in issues:
            print("    · 规则%d[%s] %s：%s" % (item["rule"], item["kind"], item["field"], item["detail"]))

    print("-" * 96)
    print("六条规则各自被多少样本触发：",
          "，".join("规则%d=%d" % (n, counter[n]) for n in range(1, 7)))
    if counter[5] == 0:
        print("提示：规则 5（记录改动）是贯穿性的，由上面每条 · 行体现，不单独触发。")

    # 可选分支（追问策略）：这里用桩函数代替真模型，不联网也能演示两条路径
    print()
    print("可选分支（追问策略）——桩函数演示，不联网：")

    def retry_good(raw, issues):
        return ('{"category":"billing","priority":"normal","amount":234.5,'
                '"reason":"重复扣款","needs_human":"no","action":"reply_only",'
                '"order_id":"2026101700012345"}')

    def retry_bad(raw, issues):
        return "还是没听懂，麻烦补充一下订单号。"

    for label, hook, expect_human in (("追问一次后成功", retry_good, False),
                                      ("追问后仍然失败", retry_bad, True)):
        result, issues, needs_human = normalize("这个工单我处理不了。", retry_fn=hook)
        ok = needs_human == expect_human
        print("  %-14s 需要人工=%-3s 动作=%-13s 结果=%s"
              % (label, "是" if needs_human else "否", result["action"],
                 "OK" if ok else "不符合预期"))
        for item in issues:
            print("      · 规则%d[%s] %s：%s"
                  % (item["rule"], item["kind"], item["field"], item["detail"]))
        if not ok:
            failed.append(label)

    if failed:
        print("不符合预期的样本：%s" % "、".join(failed))
        return 1
    print("全部样本符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
