#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_tvos_api.py 的变异测试：证明每条规则真的能红。

注入的三段代码**逐字来自 CI run #44215207929 的真实报错原文**，
不是编造的样本：

    PlayerView.swift:755:18: error: cannot override mutable property
        with read-only property 'focusEffect'
        override var focusEffect: UIFocusEffect? { nil }
    PlayerView.swift:755:31: error: 'UIFocusEffect' is unavailable in tvOS
    PlayerView.swift:775:19: error: initializer for conditional binding
        must have Optional type, not 'UIPress.PressType'
        guard let type = press.type else { continue }

如果这套规则抓不住它自己引用的那个事故，那它就是装饰。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "UnCast/Views/PlayerView.swift"
PY = sys.executable


def run() -> int:
    return subprocess.run([PY, "scripts/check_tvos_api.py"],
                          cwd=ROOT, capture_output=True, text=True).returncode


def with_patch(old: str, new: str, label: str, results: list) -> None:
    src = TARGET.read_text(encoding="utf-8")
    if old not in src:
        results.append((label, False, f"注入锚点未找到: {old[:50]!r}"))
        return
    try:
        TARGET.write_text(src.replace(old, new, 1), encoding="utf-8", newline="\n")
        rc = run()
        results.append((label, rc != 0, f"rc={rc}"))
    finally:
        TARGET.write_text(src, encoding="utf-8", newline="\n")


def main() -> int:
    base = run()
    print(f"[基线] rc={base}")
    if base != 0:
        print("基线不绿，无法做变异测试")
        return 1

    results: list[tuple[str, bool, str]] = []

    # M1: 真实事故第 1/2 条 —— 用 iOS 手段关焦点光晕
    with_patch(
        """    override func didUpdateFocus(in context: UIFocusUpdateContext,
                                 with coordinator: UIFocusAnimationCoordinator) {
        // 故意不调 super：本层没有任何需要跟着焦点变化的视觉
    }""",
        """    override var focusEffect: UIFocusEffect? { nil }""",
        "M1 回放 run #44215207929 的 focusEffect 两条错误",
        results)

    # M2: 真实事故第 3 条 —— press.type 当可选
    with_patch(
        """            switch press.type {""",
        """            guard let type = press.type else { continue }
            switch type {""",
        "M2 回放 run #44215207929 的 press.type 可选绑定错误",
        results)

    # M3: 只引用类型不用（很多人会这么试）
    with_patch(
        """        view.isUserInteractionEnabled = true""",
        """        view.isUserInteractionEnabled = true
        let _fx: UIFocusEffect? = nil""",
        "M3 仅声明 UIFocusEffect 类型",
        results)

    # M4: tvOS 17+ 才有的 SwiftUI API
    with_patch(
        """        view.isUserInteractionEnabled = true""",
        """        view.isUserInteractionEnabled = true
        _ = focusEffectDisabled()""",
        "M4 使用 tvOS 17+ 的 focusEffectDisabled()",
        results)

    # M5: 赋值形式（不是 override）
    with_patch(
        """        view.isUserInteractionEnabled = true""",
        """        view.isUserInteractionEnabled = true
        view.focusEffect = nil""",
        "M5 给 view.focusEffect 赋值",
        results)

    print()
    ok = True
    for label, hit, detail in results:
        print(f"[{'PASS' if hit else 'FAIL'}] {label}  ({detail})")
        ok &= hit

    restored = run()
    print(f"\n[还原后复跑] rc={restored}")
    ok &= restored == 0
    print("\n结论:", "规则有效（能红能绿，且抓住 CI 真实报错）"
          if ok else "存在永真规则，必须修")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())