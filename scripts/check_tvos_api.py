#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tvOS API 可用性静态扫描。

## 为什么需要它

本机没有 Xcode（Windows），编译器反馈只能靠 CI 一次来回 ——
一轮 run 要几分钟，而且失败信息埋在 zip 日志里。
2026-11-18 的 run #44215207929 就一次报了三条 tvOS/iOS 差异错误：

    PlayerView.swift:755 error: cannot override mutable property with
        read-only property 'focusEffect'
    PlayerView.swift:755 error: 'UIFocusEffect' is unavailable in tvOS
    PlayerView.swift:775 error: initializer for conditional binding must
        have Optional type, not 'UIPress.PressType'

三条都属于「iOS 上写得通、tvOS 上编译不过」。这类错误完全可以
在推送前用静态规则拦住，不必等 CI。

## 判据来源

每条规则都对应一个真实的 SDK 声明，注释里标了出处，不凭印象：

| 符号 | tvOS 状态 | 出处 |
|---|---|---|
| `UIFocusEffect` | `API_UNAVAILABLE(tvos)` | UIKit/UIFocusEffect.h |
| `UIView.focusEffect` | 同上，且 tvOS 上不可覆写 | UIKit/UIView.h:167 |
| `UIPress.type` | 非 Optional（tvOS） | UIKit/UIPress.h |
| `UIPress.PressType.*` | tvOS 全量可用 | UIKit/UIPress.h |
| `focusEffectDisabled()` | tvOS 17.0+ | SwiftUI |
| `UIPress.PressType.select` | tvOS 可用 | UIKit/UIPress.h |

## 检查方式

先剥注释再扫 —— 本项目注释里大量出现 `UIFocusEffect` 字样
（就是用来解释为什么不能用它），不剥必然全红。

只报**代码里**出现的问题，注释不算。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "UnCast"


def strip_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串与换行结构。

    不能简单用正则 `//.*` —— Swift 的字符串插值里可能有 `//`，
    以及 `https://` 这种 URL 常量。这里走状态机逐字符判断，
    和 verify_player.py 用的是同一套逻辑（那边已验证过）。
    """
    out: list[str] = []
    i, n = 0, len(src)
    in_str = in_multiline = in_line = in_block = False
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            if c == "\n":
                out.append(c)
            i += 1
            continue
        if in_multiline:
            if src[i:i + 3] == '"""':
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
            in_line = True
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


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# 规则表：(锚点说明, 命中正则, 违反说明)
# 每条都对应一个真实编译错误或明确不可用的 API。
RULES: list[tuple[str, str, str]] = [
    (
        "UIFocusEffect 引用",
        r"\bUIFocusEffect\b",
        "UIFocusEffect 在 tvOS 上是 API_UNAVAILABLE(tvos)，"
        "只有 iOS 15+ 提供；tvOS 上请改用不依赖焦点外观的写法",
    ),
    (
        "focusEffect 赋值/覆写",
        r"(?:override\s+var\s+focusEffect|\.focusEffect\s*=)",
        "UIView.focusEffect 在 tvOS 上不可覆写也不可赋值"
        "（UIKit/UIView.h 标了 API_UNAVAILABLE(tvos)）",
    ),
    (
        "press.type 当可选",
        r"guard\s+let\s+\w+\s*=\s*press\.type",
        "tvOS 的 UIPress.type 是非 Optional 的 UIPress.PressType，"
        "`guard let` 会报 initializer for conditional binding 错误",
    ),
    (
        "focusEffectDisabled（tvOS 17+）",
        # 不能写成 `\.focusEffectDisabled\(`：它既作为 modifier 链式调用
        # （`.focusEffectDisabled()`），也可能被单独引用。第一版规则漏了
        # 不带点号的情形，反向验证 M4 立刻抓到（永远为绿的规则）。
        r"\bfocusEffectDisabled\s*\(",
        "focusEffectDisabled() 需要 tvOS 17.0+，"
        "本项目 deploymentTarget 是 tvOS 15.0，直接调用会编译警告/失败",
    ),
]


def scan() -> int:
    if not SRC.is_dir():
        print(f"找不到源码目录 {SRC}")
        return 2

    files = sorted(SRC.rglob("*.swift"))
    if not files:
        print("没找到任何 .swift 文件")
        return 2

    fails: list[str] = []
    checked = 0
    for f in files:
        raw = f.read_text(encoding="utf-8")
        code = strip_comments(raw)
        for label, pattern, why in RULES:
            for m in re.finditer(pattern, code):
                ln = line_of(code, m.start())
                fails.append(
                    f"{f.relative_to(ROOT)}:{ln}  {label}\n"
                    f"    命中: {m.group(0)!r}\n"
                    f"    原因: {why}"
                )
        checked += 1

    total = len(RULES)
    print(f"扫描 {checked} 个 Swift 文件，{total} 条 tvOS API 规则")
    for label, _p, _w in RULES:
        print(f"  - {label}")

    if fails:
        print(f"\n发现 {len(fails)} 处 tvOS 不可用 API：")
        for x in fails:
            print("  ✗", x)
        print("\ntvOS API 校验: FAIL")
        return 1

    print("\ntvOS API 校验: PASS（没有使用 tvOS 上不可用的 API）")
    return 0


if __name__ == "__main__":
    sys.exit(scan())