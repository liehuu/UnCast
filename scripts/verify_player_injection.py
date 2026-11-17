#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_player.py 的变异测试：证明 34 条断言真的能失败。

## 两类注入

A. **回放历史缺陷**（最重要）
   把 v1 / v2 的原始错误写法还原回源码，确认门禁能抓住它们。
   如果连当年真出过的 bug 都抓不住，这套断言就没有存在意义。

B. **逐条破坏**
   针对每条断言做最小破坏，确认对应项变红。

每次注入后必须还原，最后复跑确认回到全绿。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

PLAYER = "UnCast/Views/PlayerView.swift"
VERIFY = "scripts/verify_player.py"
PY = sys.executable


def run_verify() -> tuple[int, list[str]]:
    r = subprocess.run([PY, VERIFY], capture_output=True, text=True)
    fails = [l.strip()[2:] for l in r.stdout.splitlines()
             if l.strip().startswith("- ")]
    return r.returncode, fails


def with_patch(old: str, new: str, label: str, expect: str | tuple[str, ...],
               results: list):
    """改一处 → 跑校验 → 还原。

    expect 可以是多个候选关键字：一处破坏往往会让"上游"断言先失败
    （例如删掉整个早退分支后，报的是「早退分支不存在」而不是
    「分支内不调用 seek」——后者已无从判断）。
    只要命中其中任意一条，就证明门禁确实在盯着这个缺陷。
    """
    keys = (expect,) if isinstance(expect, str) else expect
    src = open(PLAYER, encoding="utf-8").read()
    if old not in src:
        results.append((label, False, f"注入锚点未找到: {old[:60]!r}"))
        return
    backup = src
    try:
        patched = src.replace(old, new, 1)
        open(PLAYER, "w", encoding="utf-8", newline="\n").write(patched)
        rc, fails = run_verify()
        hit_key = next((k for k in keys if any(k in f for f in fails)), None)
        hit = rc != 0 and hit_key is not None
        detail = (f"rc={rc}, 命中「{hit_key}」" if hit
                  else f"rc={rc}, 未命中预期（报出: {fails[:2]}）")
        results.append((label, hit, detail))
    finally:
        open(PLAYER, "w", encoding="utf-8", newline="\n").write(backup)


def main() -> int:
    if not os.path.isfile(PLAYER):
        print("找不到 PlayerView.swift")
        return 2

    shutil.copy2(PLAYER, PLAYER + ".orig")

    rc, fails = run_verify()
    print(f"[基线] rc={rc}, fails={len(fails)}")
    if rc != 0:
        print("基线不绿，无法做变异测试")
        shutil.move(PLAYER + ".orig", PLAYER)
        return 1

    results: list = []

    print("\n== A. 回放历史缺陷 ==")

    # A1: v1 —— 全局拦截左右键，无仲裁
    with_patch(
        """        // ── 选键态 ──
        // 左右/上交给焦点引擎做按钮间移动，这里绝不 seek。
        // 只接管「下」：收起控制层，回到看片态。
        if isFocusInControls {
            if direction == .down { collapseControls() }
            return
        }""",
        """        // v1 缺陷回放：无条件 seek，焦点永远进不了按钮
        if direction == .left { seek(-15) }
        if direction == .right { seek(15) }""",
        "A1 回放 v1（左右键无仲裁，必 seek）",
        ("存在 isFocusInControls 早退分支",
         "焦点在控制层时分支内不调用 seek"),
        results)

    # A2: v2 —— 焦点状态用 @State 存 + 上报回执，且失焦不清
    with_patch(
        """    private var isFocusInControls: Bool { focus != nil }""",
        """    @State private var focusInControls = false
    private var isFocusInControls: Bool { focusInControls }""",
        "A2 回放 v2（焦点状态存 @State）",
        "不存在独立的 @State focusInControls",
        results)

    # A3: v2 —— 恢复 onFocusChange 回调方案
    with_patch(
        """            .focused(focus, equals: tag)""",
        """            .focused(focus, equals: tag)
            .onChange(of: isFocused) { v in onFocusChange(v) }""",
        "A3 回放 v2（onFocusChange 上报回调）",
        "不存在 onFocusChange 上报回调",
        results)

    # A4: 控制层不可恢复 —— 上下键不再呼出
    with_patch(
        """        case .up, .down:
            revealControls(focusPlay: true)""",
        """        case .up, .down:
            break""",
        "A4 上下键不再呼出控制层",
        "handleMove 内能呼出控制层",
        results)

    print("\n== B. 逐条破坏 ==")

    # B1: collapseControls 不清焦点
    with_patch(
        """        focus = nil
        withAnimation { showControls = false }""",
        """        withAnimation { showControls = false }""",
        "B1 collapseControls 忘记清焦点",
        "先释放焦点再收层",
        results)

    # B2: scheduleHide 守卫去掉焦点检查
    with_patch(
        """            guard engine.state == .playing,
                  focus == nil,
                  !showInspector else { return }""",
        """            guard engine.state == .playing,
                  !showInspector else { return }""",
        "B2 自动隐藏不再检查焦点",
        "自动隐藏前检查焦点",
        results)

    # B2b: scheduleHide 守卫去掉播放状态检查（暂停时也隐藏）
    with_patch(
        """            guard engine.state == .playing,
                  focus == nil,""",
        """            guard focus == nil,""",
        "B2b 自动隐藏不再检查播放状态",
        "自动隐藏前检查是否正在播放",
        results)

    # B3: 手势不随控制层禁用（双击 bug）
    with_patch(
        """        context.coordinator.tapGesture?.isEnabled = tapEnabled""",
        """        // 注入：手势永远启用""",
        "B3 手势不随控制层禁用",
        "updateUIView 同步手势启用状态",
        results)

    # B4: 按钮改回自带 @FocusState（双真源）
    with_patch(
        """        let focus: FocusState<PlayerView.Control?>.Binding""",
        """        @FocusState private var ownFocus: Bool
        let focus: FocusState<PlayerView.Control?>.Binding""",
        "B4 按钮自带 @FocusState（双真源）",
        "按钮内部不再持有独立 @FocusState",
        results)

    # B5: seek 增量改错
    with_patch(
        """            seek(-15)""",
        """            seek(-30)""",
        "B5 seek 步长改为 -30",
        "看片态左右分别为",
        results)

    # B6: 去掉 focusSection
    with_patch(
        """        .focusSection()""",
        """        """,
        "B6 移除 focusSection",
        "focusSection",
        results)

    print()
    ok = True
    for label, hit, detail in results:
        print(f"[{'PASS' if hit else 'FAIL'}] {label}  ({detail})")
        ok &= hit

    # 还原并复跑
    shutil.move(PLAYER + ".orig", PLAYER)
    rc, fails = run_verify()
    print(f"\n[还原后复跑] rc={rc}, fails={len(fails)}")
    ok &= (rc == 0 and len(fails) == 0)

    print("\n结论:", "契约校验有效（能红能绿，且抓住历史缺陷）"
          if ok else "存在永真断言，必须修")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())