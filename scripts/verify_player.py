#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播放器交互契约校验。

## 为什么需要这一层

方向键冲突这类问题**编译期完全看不出来**：v1 和 v2 都能 BUILD SUCCEEDED，
但 v1 按左右永远在快进（焦点永远进不了按钮），v2 焦点一旦进过按钮就
永远出不来（左右再也 seek 不了、控制层也再也不会自动隐藏）。

所以判据必须落在**源码结构**上，把"互斥状态机"这个设计意图写成可失败的断言。

## 断言的三段式要求

每条断言都必须能**失败**。scripts/verify_player_injection.py 会逐条注入
破坏，确认对应的断言确实变红——否则就是装饰。

## 检查方式

剥掉注释与字符串再匹配，避免注释里的关键词造成假 PASS
（本项目注释里大量出现 "seek"、"focus" 等词，不剥必然误判）。
"""
from __future__ import annotations

import re
import sys

PLAYER = "UnCast/Views/PlayerView.swift"


def strip_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，但保留字符串字面量里的内容不变。"""
    out = []
    i, n = 0, len(src)
    in_str = in_multiline = in_line_comment = in_block = False
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                out.append(c)
            i += 1
            continue

        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if c == "\n":
                out.append(c)   # 保留换行，维持行结构
            i += 1
            continue

        if in_multiline:
            if c == '"' and src[i:i + 3] == '"""':
                in_multiline = False
                out.append('"""')
                i += 3
                continue
            out.append(c)
            i += 1
            continue

        if in_str:
            out.append(c)
            if c == "\\":
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue

        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if src[i:i + 3] == '"""':
            in_multiline = True
            out.append('"""')
            i += 3
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out)


class Checker:
    def __init__(self):
        self.fails: list[str] = []
        self.passes = 0

    def check(self, ok: bool, msg: str, detail: str = ""):
        if ok:
            self.passes += 1
            print(f"  PASS  {msg}")
        else:
            self.fails.append(msg + (f"  [{detail}]" if detail else ""))
            print(f"  FAIL  {msg}" + (f"  [{detail}]" if detail else ""))

    def report(self, title: str) -> int:
        total = self.passes + len(self.fails)
        print(f"\n{title}: {self.passes}/{total}")
        if self.fails:
            print("失败项:")
            for f in self.fails:
                print("  -", f)
            return 1
        return 0


def extract_block(src: str, start_idx: int) -> str:
    """从 start_idx 起（或其后的第一个 '{'）按花括号配平取出整块。

    不用「按缩进匹配到某行的 }」——那种写法一旦代码重新格式化就失效，
    会变成永远找不到、或找到半截的假判据（本脚本早先就踩过）。
    """
    brace = src.find("{", start_idx)
    if brace < 0:
        return ""
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    return ""


def body_of(src: str, signature: str) -> str:
    """取函数体（按花括号配平）。找不到返回空串。"""
    idx = src.find(signature)
    if idx < 0:
        return ""
    return extract_block(src, idx)


def find_branch(src: str, condition: str) -> str:
    """取 `if <condition> { ... }` 的完整块（不含 if 头）。"""
    m = re.search(r"if\s+" + condition + r"\s*\{", src)
    if not m:
        return ""
    return extract_block(src, m.end() - 1)


def main() -> int:
    try:
        raw = open(PLAYER, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"找不到 {PLAYER}")
        return 2

    src = strip_comments(raw)
    c = Checker()

    # ─────────────────────────── 1. 状态机骨架
    print("== 1. 焦点状态必须是「单一真源」 ==")

    # 必须有 @FocusState 枚举绑定（v2 的 onFocusChange 回调方案已废弃）
    c.check("@FocusState private var focus: Control?" in src,
            "用 @FocusState 枚举承载焦点状态")

    # 焦点是否在控制层必须是纯派生，而不是存的 @State
    has_derived = bool(re.search(
        r"private var isFocusInControls: Bool\s*\{\s*focus\s*!=\s*nil\s*\}", src))
    c.check(has_derived, "isFocusInControls 是派生计算属性（focus != nil）")

    # 反向：不能再有独立存储的 focusInControls 状态
    stored = re.search(r"@State\s+private\s+var\s+focusInControls", src)
    c.check(stored is None,
            "不存在独立的 @State focusInControls（防状态残留）")

    # 反向：不能再有上报回调式的焦点同步
    callback = re.search(r"onFocusChange", src)
    c.check(callback is None, "不存在 onFocusChange 上报回调（v2 缺陷来源）")

    # ─────────────────────────── 2. 方向键仲裁
    print("\n== 2. 方向键仲裁必须互斥 ==")
    handle = body_of(src, "private func handleMove")
    c.check(bool(handle), "handleMove 存在")
    if handle:
        # 2a. 焦点在按钮上时，左右必须**不**seek
        #     结构判据：isFocusInControls 分支在 seek 调用之前 return
        m = re.search(
            r"if\s+isFocusInControls\s*\{([\s\S]*?)\n        \}", handle)
        c.check(m is not None, "存在 isFocusInControls 早退分支")
        if m:
            branch = m.group(1)
            c.check("seek(" not in branch,
                    "焦点在控制层时分支内不调用 seek（左右键让给焦点引擎）",
                    branch.strip()[:80])
            c.check("return" in branch, "该分支以 return 早退")

        # 2b. seek 只能出现在看片态
        seek_calls = [x for x in re.findall(r"\bseek\((-?\d+)\)", handle)]
        c.check(seek_calls == ["-15", "15"],
                "看片态左右分别为 -15 / +15 秒", str(seek_calls))

        # 2c. 上下键必须能唤回控制层（"一旦隐藏就调不出来"的解药）
        c.check("revealControls" in handle,
                "handleMove 内能呼出控制层")
        c.check(re.search(r"case \.up, \.down", handle) is not None,
                "上下键合并为同一分支（都呼出控制层）")

    # ─────────────────────────── 3. 控制层可恢复性
    print("\n== 3. 控制层隐藏后必须可恢复 ==")

    reveal = body_of(src, "private func revealControls")
    c.check(bool(reveal), "revealControls 存在")
    if reveal:
        c.check("showControls = true" in reveal,
                "revealControls 置 showControls = true")

    collapse = body_of(src, "private func collapseControls")
    c.check(bool(collapse), "collapseControls 存在")
    if collapse:
        c.check("focus = nil" in collapse,
                "collapseControls 先释放焦点再收层")
        c.check("showControls = false" in collapse,
                "collapseControls 置 showControls = false")

    # 自动隐藏的三条守卫必须都在，且不能依赖"会残留"的状态。
    #
    # 这里只认**行为**，不认写法：`focus == nil` 与 `!isFocusInControls`
    # 语义完全等价，用字面量去卡其中一种就是过度拟合实现文本，
    # 会在合法重构时报假 FAIL（本脚本早先就犯过）。
    hide = body_of(src, "private func scheduleHide")
    c.check(bool(hide), "scheduleHide 存在")
    if hide:
        has_focus_guard = (re.search(r"focus\s*==\s*nil", hide) is not None
                           or re.search(r"!\s*isFocusInControls", hide) is not None)
        c.check(has_focus_guard,
                "自动隐藏前检查焦点（否则用户正在选按钮时面板会消失）",
                hide.strip()[:100])
        c.check(re.search(r"engine\.state\s*==\s*\.playing", hide) is not None,
                "自动隐藏前检查是否正在播放（暂停时不隐藏）")
        c.check("showInspector" in hide,
                "自动隐藏前检查检查器是否打开")
        # 反向：绝不能因为某个"存起来的状态"永久挡住隐藏
        c.check("focusInControls" not in hide,
                "守卫不引用已废弃的存储态 focusInControls")

    # 必须有 UIKit 点按通道：tvOS 15 无 onTapGesture 点击语义
    c.check("UITapGestureRecognizer" in src,
            "用 UITapGestureRecognizer 提供点按（兼容 tvOS 15）")
    c.check("allowedPressTypes" in src,
            "手势限定 select 按键")

    # 关键：手势必须在控制层显示时禁用，否则确定键被抢
    c.check(re.search(r"tap\.isEnabled\s*=\s*tapEnabled", src) is not None,
            "手势启用状态跟随 tapEnabled")
    c.check(re.search(r"tapGesture\?\.isEnabled\s*=\s*tapEnabled", src) is not None,
            "updateUIView 同步手势启用状态")
    c.check("tapEnabled: !showControls" in src,
            "调用处按 showControls 反向绑定（显示时禁用）")

    # ─────────────────────────── 4. 焦点与按钮绑定
    print("\n== 4. 按钮焦点必须绑定同一 FocusState ==")

    btn = body_of(src, "struct CircleButton")
    c.check(bool(btn), "CircleButton 存在")
    if btn:
        c.check("FocusState<PlayerView.Control?>.Binding" in btn,
                "按钮接收父视图的 FocusState 绑定")
        c.check(re.search(r"\.focused\(focus,\s*equals:\s*tag\)", btn) is not None,
                "用 .focused(_:equals:) 绑定（而非独立 @FocusState）")
        # 反向：不能再有按钮自己的 @FocusState（会导致双源）
        c.check("@FocusState" not in btn,
                "按钮内部不再持有独立 @FocusState")

    # 调用处必须把所有按钮绑到 $focus
    call_count = len(re.findall(r"CircleButton\(", src))
    bound = len(re.findall(r"focus:\s*\$focus", src))
    c.check(call_count > 0 and call_count == bound,
            f"{call_count} 个按钮全部传入 $focus", f"实际绑定 {bound}")

    # 按钮行必须是独立 focusSection，否则会和控制层外的元素混排
    c.check(".focusSection()" in src, "按钮行声明 focusSection")

    # ─────────────────────────── 5. 无残留死代码
    print("\n== 5. 无冲突残留 ==")
    for dead in ("ClickToToggleModifier", "ActivateToToggleModifier", "TapCatcher"):
        c.check(dead not in src, f"已移除旧手势方案 {dead}")

    # showControls 变化时必须清焦点
    c.check(re.search(
        r"\.onChange\(of:\s*showControls\)[\s\S]{0,200}?focus\s*=\s*nil", src)
        is not None,
        "showControls 变 false 时清空焦点")

    return c.report("播放器交互契约")


if __name__ == "__main__":
    sys.exit(main())