#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_player.py 的变异测试：证明每条断言真的能失败。

## 两类注入

A. **回放历史缺陷**（最重要）
   把 v1 / v2 / v3 的原始错误写法还原回源码，确认门禁能抓住它们。
   如果连当年真出过的 bug 都抓不住，这套断言就没有存在意义。

   v3 的缺陷特别值得回放：它把方向键挂在 `onMoveCommand` 上，
   而控制层隐藏时没有任何可聚焦元素 —— 那段
   `case .up, .down: revealControls()` 是死代码。静态读代码完全正常，
   只有真机上发现"按键毫无反应"才会暴露。所以 v4 必须把断言钉在
   "存在常驻焦点锚点 + pressesBegan 接管"上，而不是钉在
   "handleMove 里有 reveal 分支"上 —— 后者 v3 就满足，却是有病的。

B. **逐条破坏**
   针对每条断言做最小破坏，确认对应项变红。

C. **图标 plist 格式**（check_icon.py 的变异）
   tvOS 的 CFBundlePrimaryIcon 必须是字符串，写成 iOS 的字典形式
   编译照样成功、actool 也不报错，但主屏白块 —— CI run #31 真栽过。

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
    （例如删掉整个分支后，报的是「分支不存在」而不是「分支内做了什么」
    —— 后者已无从判断）。
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

    # A1: v3 致命缺陷 —— 删掉焦点锚点，方向键再也进不来。
    #     这就是用户报的"控制层一旦自动隐藏就调不出来"。
    #     注意：此时 handleMove 里的 up/down 分支依然完整存在，
    #     静态逻辑无懈可击，但按键永远到不了 —— 正因如此，
    #     断言必须钉在锚点上，不能钉在分支上。
    with_patch(
        """    override var canBecomeFocused: Bool { capturesKeys }""",
        """    override var canBecomeFocused: Bool { false }""",
        "A1 回放 v3（锚点不可聚焦，方向键永不派发）",
        "canBecomeFocused 是动态的",
        results)

    # A2: v3 —— 只保留 onMoveCommand，不用 pressesBegan
    with_patch(
        """    override func pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {""",
        """    override func _removed_pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {""",
        "A2 回放 v3（按钮不接管 pressesBegan）",
        "在 pressesBegan 里接管方向键",
        results)

    # A3: v1 —— 按钮态也霸占焦点，左右永远在 seek
    with_patch(
        """    override var canBecomeFocused: Bool { capturesKeys }""",
        """    override var canBecomeFocused: Bool { true }""",
        "A3 回放 v1（锚点常驻可聚焦，按钮永远聚焦不上）",
        "canBecomeFocused 是动态的",
        results)

    # A4: v2 —— 焦点另存一份本地状态（双真源）。
    #     实测教训：早先断言写的是 `"@FocusState" not in btn`，
    #     而注入的是 `@State`——子串不含，断言一次都没红过。
    #     现在断言同时盯「有无任何可变属性包裹器」与「isFocused 是否只由
    #     父级绑定决定」，两种写法都能抓住。
    with_patch(
        """        private var isFocused: Bool { focus.wrappedValue == tag }""",
        """        @State private var ownFocusFlag = false
        private var isFocused: Bool { focus.wrappedValue == tag || ownFocusFlag }""",
        "A4 回放 v2（按钮内部存 @State 焦点）",
        ("按钮内部不持有任何可变焦点状态", "isFocused 只由父级绑定决定"),
        results)

    # A4b: v2 的等价写法 —— 用 @FocusState 自持（原断言唯一能抓的形态，
    #      保留它，防止修 A4 时把这半边的覆盖丢掉）
    with_patch(
        """        private var isFocused: Bool { focus.wrappedValue == tag }""",
        """        @FocusState private var inner: Bool
        private var isFocused: Bool { focus.wrappedValue == tag || inner }""",
        "A4b 回放 v2（按钮内部 @FocusState 自持）",
        ("按钮内部不持有任何可变焦点状态", "isFocused 只由父级绑定决定"),
        results)

    # A5: v3 —— 把三级压回布尔量，进度条/按钮不再分离
    with_patch(
        """    @State private var level: Level = .progress""",
        """    @State private var level: Level = .progress
    @State private var showControls = true""",
        "A5 回放 v3（同时保留 showControls 布尔量）",
        "不再存在 showControls 布尔量",
        results)

    print("\n== B. 逐条破坏 ==")

    # B1: 下键不再展开到按钮层（停在进度条）。
    #     早先断言查 `".progress" in down_body and ".buttons" in down_body`，
    #     掏空这一格后 `case .buttons:` 这个标签仍含 ".buttons" —— 假 PASS。
    #     现在断言精确切到 `.progress` 那一格里找赋值语句。
    with_patch(
        """            case .progress:
                withAnimation { level = .buttons }""",
        """            case .progress:
                break""",
        "B1 下键不再展开按钮层",
        ("下键逐级展开到按钮层", "下键用三级 switch 仲裁"),
        results)

    # B1b: 把整个 .down 分支的反向分支删掉（下键语义退化成布尔反转）
    with_patch(
        """            case .hidden:
                withAnimation { level = .progress }
            case .progress:
                withAnimation { level = .buttons }""",
        """            case .hidden:
                withAnimation { level = .buttons }
            case .progress:
                withAnimation { level = .buttons }""",
        "B1b 下键跳过进度条直接进按钮",
        "下键在隐藏态呼出进度条",
        results)

    # B2: 上键在隐藏态不呼出。
    #     同上，`case .progress, .buttons:` 含 ".progress"，
    #     掏空 `.hidden` 那格后子串判断依然是绿的。
    with_patch(
        """            case .hidden:
                withAnimation { level = .progress }
            case .progress, .buttons:
                withAnimation { level = .hidden }""",
        """            case .hidden:
                break
            case .progress, .buttons:
                withAnimation { level = .hidden }""",
        "B2 上键在隐藏态不呼出进度条",
        "上键在隐藏态也能呼出进度条",
        results)

    # B2b: 当前用户报的那个 bug 本身 —— 上下键完全不响应
    with_patch(
        """        case .up:
            // **上下键都能呼出进度条**（用户明确要求）。
            // 已经在控制层里时，上 = 收起。
            switch level {
            case .hidden:
                withAnimation { level = .progress }
            case .progress, .buttons:
                withAnimation { level = .hidden }
            }""",
        """        case .up:
            break""",
        "B2b 回放用户报的 bug（上键完全失效）",
        ("handleMove 有 .up 分支", "上键在隐藏态也能呼出进度条"),
        results)

    # B3: 自动隐藏不再检查播放状态
    with_patch(
        """            guard engine.state == .playing else { return }""",
        """            // 注入：不检查播放状态""",
        "B3 自动隐藏不再检查播放状态",
        "自动隐藏前检查是否正在播放",
        results)

    # B4: 自动隐藏不再检查按钮焦点
    with_patch(
        """            if level == .buttons && focus != nil { return }""",
        """            // 注入：不检查焦点""",
        "B4 自动隐藏不再检查按钮焦点",
        "自动隐藏前检查按钮焦点",
        results)

    # B5: 手势永远启用（双击 bug）
    with_patch(
        """        context.coordinator.tapGesture?.isEnabled = tapEnabled""",
        """        // 注入：手势永远启用""",
        "B5 手势不随控制层禁用",
        "updateUIView 同步手势启用状态",
        results)

    # B6: 进度条层塞进按钮行（三级退化成两级）
    with_patch(
        """    private var progressLayer: some View {
        VStack {
            Spacer()
            progressBar""",
        """    private var progressLayer: some View {
        VStack {
            Spacer()
            progressBar
            controlButtons""",
        "B6 进度条层混入按钮行",
        "进度条层不含按钮行",
        results)

    # B7: seek 步长改错
    with_patch(
        """            seek(-15)""",
        """            seek(-30)""",
        "B7 seek 步长改为 -30",
        "左右分别为",
        results)

    # B8: 去掉 focusSection
    with_patch(
        """        .focusSection()""",
        """        """,
        "B8 移除 focusSection",
        "focusSection",
        results)

    # B9: 焦点丢失兜底被删（再次陷入无焦点死区）
    with_patch(
        """            if level == .buttons && newFocus == nil {
                withAnimation { level = .progress }
            }""",
        """            // 注入：焦点丢失后不兜底""",
        "B9 焦点丢失后不退回进度条态",
        "焦点意外丢失时退回进度条态",
        results)

    # B10: 切回锚点态不主动要焦点。
    #     源码里 setNeedsFocusUpdate 有两处（updateUIView 与
    #     didMoveToWindow），早先断言查"全文有没有这个词"，
    #     删掉 updateUIView 这处仍命中另一处 —— 假 PASS。
    #     现在断言只在 updateUIView 函数体内找。
    #     这里注入的是「条件退化成每帧都要焦点」——它不会少要焦点，
    #     反而每帧都要一次，表现是焦点被反复重置、按钮点不动。
    #     专门用来验证第二条断言（跃迁判定）不是装饰。
    with_patch(
        """        if capturesKeys && !wasCapturing {""",
        """        if capturesKeys {""",
        "B10 跃迁判定退化成每帧都要焦点",
        "只在「按钮态 → 锚点态」",
        results)

    # B10b: 只删焦点请求、保留跃迁判定（不引爆上游断言，只能靠这一条抓）
    with_patch(
        """            uiView.setNeedsFocusUpdate()
            uiView.updateFocusIfNeeded()""",
        """            _ = wasCapturing""",
        "B10b 只删焦点请求语句",
        "切回锚点态时主动请求焦点更新",
        results)

    # B11: 按钮态不放行按键（按钮永远点不动）
    with_patch(
        """        guard capturesKeys else {""",
        """        guard true else {""",
        "B11 pressesBegan 不放行按键",
        "pressesBegan 在非捕获态放行按键",
        results)

    # B12: 焦点光晕没关（整屏视频一圈白光）
    with_patch(
        """    override var focusEffect: UIFocusEffect? { nil }""",
        """    override var focusEffect: UIFocusEffect? { .automatic }""",
        "B12 焦点光晕未关闭",
        "focusEffect 为 nil",
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

    # ── C. 图标 plist 格式（真实踩过的坑）
    ok &= check_plist_shape()

    print("\n结论:", "契约校验有效（能红能绿，且抓住历史缺陷）"
          if ok else "存在永真断言，必须修")
    return 0 if ok else 1


def check_plist_shape() -> bool:
    """tvOS 的 CFBundlePrimaryIcon 必须是字符串。
    写成 iOS 的字典形式时构建全绿但主屏白块 —— CI run #31 的真实失败原因。"""
    print("\n== C. 图标 plist 格式（iOS 字典 vs tvOS 字符串）==")
    plist = "UnCast/App/Info.plist"
    if not os.path.isfile(plist):
        print("[FAIL] 找不到 Info.plist")
        return False

    import plistlib
    shutil.copy2(plist, plist + ".orig")

    def icon_rc():
        r = subprocess.run([PY, "scripts/check_icon.py", "."],
                           capture_output=True, text=True)
        return r.returncode

    try:
        base = icon_rc()
        info = plistlib.load(open(plist, "rb"))
        info["CFBundleIcons"]["CFBundlePrimaryIcon"] = {
            "CFBundleIconName": "App Icon",
            "CFBundleIconFiles": ["AppIcon"],
        }
        with open(plist, "wb") as f:
            plistlib.dump(info, f)
        injected = icon_rc()
    finally:
        shutil.move(plist + ".orig", plist)

    restored = icon_rc()
    hit = base == 0 and injected != 0 and restored == 0
    print(f"[{'PASS' if hit else 'FAIL'}] iOS 字典形式被拒绝  "
          f"(基线 rc={base}, 注入 rc={injected}, 还原 rc={restored})")
    return hit


if __name__ == "__main__":
    sys.exit(main())