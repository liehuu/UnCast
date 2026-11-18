#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播放器交互契约校验。

## 为什么需要这一层

方向键冲突这类问题**编译期完全看不出来**：v1 和 v2 都能 BUILD SUCCEEDED，
但 v1 按左右永远在快进（焦点永远进不了按钮），v2 焦点一旦进过按钮就
永远出不来（左右再也 seek 不了、控制层也再也不会自动隐藏）。

v3 更隐蔽：它把方向键处理挂在 SwiftUI 的 `onMoveCommand` 上，而
`onMoveCommand` **依赖焦点**。控制层隐藏时 `focus = nil`，视图树里没有
任何可聚焦元素，tvOS 焦点引擎无处派发 —— 于是
`case .up, .down: revealControls()` 那段代码**一次都不会执行**。
分支写着"上下键呼出控制层"，静态看起来完全正确，只有真机上按键
毫无反应才暴露。这是"控制层调不回来"和"方向键控不了进度"的
同一个根因：按键根本没进来。

## v4 的契约

1. **常驻焦点锚点**：承载视频的全屏 UIView 自己 `canBecomeFocused`，
   在非按钮态持有焦点，方向键经 `pressesBegan` 进入。
2. **三级累进**：hidden → progress → buttons，每级按键含义不重叠。
3. **互斥**：按钮态必须关掉 `canBecomeFocused` 与 `capturesKeys`，
   否则全屏锚点会一直霸占焦点，按钮永远聚焦不上。

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


def _case_marks(inner: str) -> list[tuple[int, int, str]]:
    """扫出一层 switch 里所有 case 的 (起始偏移, 冒号偏移, 标签)。

    只看花括号深度 0 —— 嵌套 switch 里的 case 属于别的 switch，
    混进来会把外层分支切碎（本脚本第一版就这么错过了 B1/B2）。
    """
    marks: list[tuple[int, int, str]] = []
    depth = 0
    i, n = 0, len(inner)
    while i < n:
        ch = inner[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if depth == 0 and inner.startswith("case ", i):
            j = i - 1
            while j >= 0 and inner[j] in " \t":
                j -= 1
            if j < 0 or inner[j] == "\n":          # 必须是行首（前面只有空白）
                colon = inner.find(":", i)
                brk = inner.find("\n", i)
                if colon > 0 and (brk < 0 or colon < brk):
                    marks.append((i, colon, inner[i + 5:colon].strip()))
                    i = colon + 1
                    continue
        i += 1
    return marks


def switch_arms(text: str) -> list[tuple[str, str]]:
    """把文本里**第一个** switch 拆成 [(标签, 分支体), ...]。

    为什么要这个函数：`"level = .buttons" in down_body` 这种子串判断
    会被同层 `case .buttons:` 这个**标签本身**命中 —— 把 `.progress`
    臂掏空成 `break` 之后标签还在，断言永远是绿的。本项目第一版变异
    测试的 B1/B2 就是这么假 PASS 的。判据必须精确到「那一格」。
    """
    idx = text.find("switch")
    if idx < 0:
        return []
    block = extract_block(text, idx)          # 含首尾花括号
    if not block:
        return []
    inner = block[1:-1]
    marks = _case_marks(inner)
    arms: list[tuple[str, str]] = []
    for k, (_start, colon, label) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(inner)
        arms.append((label, inner[colon + 1:end]))
    return arms


def case_arm(text: str, label: str) -> str:
    """取第一个 switch 里某个 case 的整臂（含它自己的嵌套 switch）。"""
    for lab, body in switch_arms(text):
        if lab == label:
            return body
    return ""


def main() -> int:
    try:
        raw = open(PLAYER, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"找不到 {PLAYER}")
        return 2

    src = strip_comments(raw)
    c = Checker()

    # ─────────────────────────── 1. 焦点锚点（v4 核心）
    print("== 1. 必须存在常驻焦点锚点（否则方向键永远收不到）==")

    # 这是 v3「控制层调不回来」的根因：没有可聚焦元素，焦点引擎
    # 不派发方向键，onMoveCommand 永不触发。
    #
    # 允许两种实现：
    #   A. UIKit 的 canBecomeFocused（本版）
    #   B. SwiftUI 的 .focusable()（需同时关掉焦点外观）
    has_uikit_anchor = "override var canBecomeFocused" in src
    has_swiftui_anchor = re.search(r"\.focusable\(", src) is not None
    c.check(has_uikit_anchor or has_swiftui_anchor,
            "存在焦点锚点（canBecomeFocused 或 .focusable）")

    # 锚点必须是**动态**的：按钮态要能让位
    c.check(re.search(
        r"override\s+var\s+canBecomeFocused\s*:\s*Bool\s*\{\s*capturesKeys\s*\}",
        src) is not None,
        "canBecomeFocused 是动态的（按钮态让位给按钮）")

    # 方向键必须从 pressesBegan 进，不能只靠 onMoveCommand
    c.check("override func pressesBegan" in src,
            "在 pressesBegan 里接管方向键（不依赖焦点派发的 onMoveCommand）")

    pb = body_of(src, "override func pressesBegan")
    c.check(bool(pb), "pressesBegan 有实现")
    if pb:
        for key in (".upArrow", ".downArrow", ".leftArrow", ".rightArrow"):
            c.check(key in pb, f"pressesBegan 处理 {key}")
        # tvOS 的 UIPress.type 是非可选的，写 `guard let type = press.type`
        # 直接编译失败（CI run #44215207929 真实报错）。断言必须拦住它，
        # 否则源码一旦回退成 iOS 写法，Gate 全绿但编译红。
        c.check(re.search(r"guard\s+let\s+\w+\s*=\s*press\.type", pb) is None,
                "不把 press.type 当可选（tvOS 上非 Optional，guard let 编译不过）")

    # 关键互斥：按钮态必须放行按键，否则按钮永远聚焦不上
    c.check(re.search(r"guard\s+capturesKeys\s+else\s*\{", pb) is not None,
            "pressesBegan 在非捕获态放行按键（按钮态让位焦点引擎）",
            pb.strip()[:80] if pb else "")

    # 焦点外观必须关掉，否则整屏视频会有一圈白光晕。
    #
    # 注意 tvOS 的现实（CI run #44215207929 真实失败）：
    #   - `UIFocusEffect` 是 API_UNAVAILABLE(tvos)，只有 iOS 15+ 有
    #   - `UIView.focusEffect` 在 tvOS 上不可覆写
    # 所以绝不能把 `override var focusEffect: UIFocusEffect? { nil }`
    # 钉成"必须存在"——那等于把源码逼到编译不过。
    # tvOS 上真正成立的判据是：不引入任何焦点外观 API，
    # 并且堵住 didUpdateFocus 的动画协调器。
    c.check(re.search(r"\bUIFocusEffect\b", src) is None,
            "不引用 UIFocusEffect（tvOS 上 API_UNAVAILABLE，写了就编译不过）")
    c.check(re.search(r"\bfocusEffect\s*=", src) is None,
            "不给 view 设焦点效果（裸 UIView 本就不画光晕）")
    c.check("override func didUpdateFocus" in src,
            "覆写 didUpdateFocus 堵住焦点动画协调器（全屏视频不画光晕）")

    # ─────────────────────────── 2. 三级累进状态机
    print("\n== 2. 控制层必须是三级累进 ==")

    c.check(re.search(r"enum\s+Level\s*:\s*Int", src) is not None,
            "存在 Level 枚举（三级累进）")
    for case in ("case hidden", "case progress", "case buttons"):
        c.check(case in src, f"Level 含 {case}")

    # 两个控制层必须分别存在，不能合并成一个
    c.check(bool(body_of(src, "private var progressLayer")),
            "存在独立的进度条层 progressLayer")
    c.check(bool(body_of(src, "private var buttonsLayer")),
            "存在独立的按钮层 buttonsLayer")

    # 进度条层里**不能**有按钮；这是"按一下只看进度"的前提
    pl = body_of(src, "private var progressLayer")
    if pl:
        c.check("controlButtons" not in pl,
                "进度条层不含按钮行（用户按一下就只看进度）")

    # ─────────────────────────── 3. 方向键仲裁
    print("\n== 3. 方向键语义必须互斥 ==")
    handle = body_of(src, "private func handleMove")
    c.check(bool(handle), "handleMove 存在")
    if handle:
        # 左右键传的是**方向**（±1），距离由连按档位决定。
        #
        # 早先断言写的是「左右分别为 -15 / +15」，改成累进后这条必然失败。
        # 但顺手暴露了一个设计问题：如果按键处写 15、算法里又改成 60，
        # 就是双真源。现在档位只在 seekSteps 一处定义，按键只传方向。
        seek_calls = re.findall(r"\bseek\((-?\d+)\)", handle)
        c.check(seek_calls == ["-1", "1"],
                "左右键只传方向 ±1（距离由连按档位单点定义）", str(seek_calls))

        # 下：hidden → progress → buttons（逐级展开）
        down_body = case_arm(handle, ".down")
        c.check(bool(down_body), "handleMove 有 .down 分支")
        if down_body:
            # 逐级展开必须落在**各自那一格**里。
            # 不能只看 down_body 里有没有 ".progress" / ".buttons"：
            # `case .progress:` 这个标签本身含 ".progress"，
            # 把该格掏空成 break 后子串照样命中 —— 假 PASS。
            d_arms = dict(switch_arms(down_body))
            c.check(".hidden" in d_arms and ".progress" in d_arms,
                    "下键用三级 switch 仲裁（不是布尔量取反）",
                    str(list(d_arms)))
            c.check("level = .progress" in d_arms.get(".hidden", ""),
                    "下键在隐藏态呼出进度条",
                    d_arms.get(".hidden", "").strip()[:60])
            c.check("level = .buttons" in d_arms.get(".progress", ""),
                    "下键逐级展开到按钮层",
                    d_arms.get(".progress", "").strip()[:60])

        # 上：hidden 时也要能呼出进度条（用户明确要求）
        up_body = case_arm(handle, ".up")
        c.check(bool(up_body), "handleMove 有 .up 分支")
        if up_body:
            u_arms = dict(switch_arms(up_body))
            c.check("level = .progress" in u_arms.get(".hidden", ""),
                    "上键在隐藏态也能呼出进度条",
                    u_arms.get(".hidden", "").strip()[:60])
            # 只有进度条态才由 UIKit 这条路收起；按钮态已由
            # handleMove 顶部的 guard 整体挡掉（收起走 onMoveCommand），
            # 所以这里**不能**再要求 `.progress, .buttons` 合并分支。
            c.check("level = .hidden" in u_arms.get(".progress", ""),
                    "上键在进度条态收起",
                    u_arms.get(".progress", "").strip()[:60])
            c.check("level = .hidden" not in u_arms.get(".buttons", ""),
                    "上键在按钮态不由 UIKit 路径收起（避免同一次按键双处理）",
                    u_arms.get(".buttons", "").strip()[:60])

    # ─────────────────────────── 3.5 连按累进 seek
    print("\n== 3.5 左右键连按累进（15s → 1min → 5min）==")

    # 档位表必须存在且顺序正确。这条不能只查"出现了 15/60/300"，
    # 因为顺序错（比如 [15, 300, 60]）子串判断照样过。
    m = re.search(r"seekSteps\s*(?::\s*\[Double\]\s*)?=\s*\[([^\]]+)\]", src)
    c.check(m is not None, "存在 seekSteps 档位表")
    if m:
        vals = [v.strip().rstrip("dD") for v in m.group(1).split(",")]
        c.check(vals == ["15", "60", "300"],
                "档位顺序为 15 / 60 / 300 秒", str(vals))

    # 档位表里不能夹带负数或 0（否则会算出"快速反向跳"这种怪行为）
    if m:
        nums = [float(v) for v in vals if re.fullmatch(r"\d+(?:\.\d+)?", v)]
        c.check(len(nums) == 3 and all(n > 0 for n in nums),
                "档位全为正数（方向由符号单独决定）", str(nums))

    # 第 3 次之后要继续递进，不能停在原地。
    # 停住的话用户第 4 次按下会看到同一个 5 分钟提示，以为按键失灵。
    c.check(re.search(r"seekTailStep\s*:\s*Double\s*=\s*300", src) is not None,
            "第 3 档之后有递进步长 seekTailStep")
    offset_fn = body_of(src, "static func seekOffset")
    c.check(bool(offset_fn), "存在 seekOffset 档位计算函数")
    if offset_fn:
        c.check(re.search(r"streak\s*<=\s*seekSteps\.count", offset_fn) is not None,
                "档位表内按索引取值")
        c.check(re.search(r"streak\s*-\s*seekSteps\.count", offset_fn) is not None,
                "超出档位表后按次数递进（不是原地停在最后一档）")

    seekfn = body_of(src, "private func seek(")
    c.check(bool(seekfn), "seek 函数存在")
    if seekfn:
        # 累进的核心是「记住序列起点」，不能基于 currentTime 反复相对累加。
        # 理由：seek 是异步的，连按时 currentTime 还没更新，相对累加会少跳。
        #
        # 判据必须钉在**赋值动作**上，不能只查 `"seekAnchor" in seekfn`：
        # 变量名在 `seekAnchor ?? ...`、`seekAnchor != nil` 里都出现，
        # 把 `seekAnchor = engine.currentTime` 删掉后子串依然命中 ——
        # 变异测试 C3 就是这么报假 PASS 的。
        c.check(re.search(r"seekAnchor\s*=\s*engine\.currentTime", seekfn) is not None,
                "把 engine.currentTime 存进 seekAnchor（序列起点）")
        c.check("engine.seek(to:" in seekfn,
                "走绝对定位 engine.seek(to:)（不是 seek(by:)）")
        c.check("engine.seek(by:" not in seekfn,
                "不基于 currentTime 做相对累加")
        # 方向必须参与「是否延续序列」的判断。
        # 只查 `"seekSign" in seekfn` 同样无效：`seekSign = dir` 让它永远为真。
        c.check(re.search(r"seekSign\s*==\s*dir", seekfn) is not None,
                "换方向时重新起算（seekSign == dir 参与延续判断）")
        c.check(re.search(r"seekStreak\s*\+=\s*1", seekfn) is not None,
                "每次按键累加连按次数")
        # 越界夹紧
        c.check(re.search(r"min\(max\(0,\s*anchor\s*\+\s*delta\)", seekfn) is not None,
                "目标位置夹在 [0, duration] 内")
        # 连按窗口超时后必须复位，否则下一个序列会从 5 分钟起跳
        c.check("armSeekStreakTimeout" in seekfn,
                "每次按键都重置连按窗口计时")

    timeout_fn = body_of(src, "private func armSeekStreakTimeout")
    c.check(bool(timeout_fn), "存在连按窗口超时函数")
    if timeout_fn:
        # 复位必须是「赋回初始值」，不能只查变量名后面跟了个等号 ——
        # 注入 `_ = seekStreak` 这种"读一下但不改"的写法就能骗过旧断言。
        c.check(re.search(r"seekStreak\s*=\s*0\b", timeout_fn) is not None,
                "超时后把 seekStreak 归零（否则下次从大档位起跳）")
        c.check(re.search(r"seekSign\s*=\s*0\b", timeout_fn) is not None,
                "超时后把 seekSign 归零（否则方向判定的延续条件永远成立）")
        c.check(re.search(r"seekAnchor\s*=\s*nil\b", timeout_fn) is not None,
                "超时后把 seekAnchor 置 nil（否则沿用过期的起点）")
        # 还要确认真的 sleep 了窗口时长，否则立即复位等于连按永远无效
        c.check(re.search(r"Task\.sleep\(nanoseconds:\s*Self\.seekStreakWindow",
                          timeout_fn) is not None,
                "真正等待连按窗口时长后再复位")

    # 按钮行的 ±15 按钮也要走同一入口，否则遥控器与按钮行为不一致
    cb = body_of(src, "private var controlButtons")
    if cb:
        c.check("engine.seek(by: -15)" not in cb and "engine.seek(by: 15)" not in cb,
                "按钮行的快退/快进也走 seek() 入口（与方向键一致）")

    # ─────────────────────────── 3.6 按钮态的上键回退
    print("\n== 3.6 按钮行按上要退回进度条 ==")

    # 按钮态方向键由焦点引擎派发到 onMoveCommand，锚点此时已让位。
    # 所以「上回退」必须写在 onMoveCommand 里，写进 handleMove 是死代码
    # （按钮态 capturesKeys=false，pressesBegan 直接放行，handleMove 收不到）。
    move_cmd = body_of(src, ".onMoveCommand")
    c.check(bool(move_cmd), "存在 onMoveCommand（按钮态的方向键入口）")
    if move_cmd:
        c.check(re.search(r"guard\s+level\s*==\s*\.buttons", move_cmd) is not None,
                "onMoveCommand 只在按钮态生效（其余两态由 UIKit 层处理）")
        # 精确切分支，避免子串被 case 标签本身命中
        mc_arms = dict(switch_arms(move_cmd))
        c.check("level = .progress" in mc_arms.get(".up", ""),
                "按钮态按上退回进度条态",
                mc_arms.get(".up", "").strip()[:60])
        c.check("level = .hidden" in mc_arms.get(".down", ""),
                "按钮态按下收起整个控制层",
                mc_arms.get(".down", "").strip()[:60])

        # 「下键收起」必须带防重派发窗口。
        #
        # 触发进入按钮态的那一次物理按下，可能在状态切换后被焦点引擎
        # 再派发一次；此时 level 已是 .buttons，同一次按键就既展开又收起，
        # 用户看到的是按钮行闪灭。加时间窗挡掉这一次。
        c.check("collapseGuardUntil" in mc_arms.get(".down", ""),
                "下键收起带防重复派发窗口（同一次按键不会既展开又收起）",
                mc_arms.get(".down", "").strip()[:80])

    # handleMove 必须在按钮态整体不动作。
    #
    # 这是「按钮行连进度条一起闪灭」的根因所在：`level = .buttons` 同步
    # 写 @State，但锚点的 `capturesKeys = false` 要等下一次 updateUIView
    # 才落地；滞后窗口内同一次下键又被送进 handleMove，而旧代码把
    # `.progress, .buttons` 合并成「上 = 收起」，于是按一下就展开又收起。
    hm = body_of(src, "private func handleMove")
    c.check(bool(hm), "handleMove 存在")
    if hm:
        c.check(re.search(r"guard\s+level\s*!=\s*\.buttons\s+else\s*\{\s*return\s*\}",
                          hm) is not None,
                "handleMove 在按钮态整体让位（不处理任何方向键）")
        # 兜底分支也不许收起整个控制层
        c.check(len(re.findall(r"level\s*=\s*\.hidden", hm)) <= 1,
                "handleMove 里至多一处收起（仅进度条态），按钮态不收起")

    # ─────────────────────────── 4. 控制层可恢复性
    print("\n== 4. 控制层隐藏后必须可恢复 ==")

    # 隐藏态必须是 level = .hidden 而不是 showControls = false —— 后者
    # 是布尔量，表达不了三级
    c.check(re.search(r"@State\s+private\s+var\s+level\s*:\s*Level\s*=", src) is not None,
            "level 用 @State 承载")

    restart = body_of(src, "private func restartHide")
    c.check(bool(restart), "restartHide 存在")
    if restart:
        c.check(re.search(r"engine\.state\s*==\s*\.playing", restart) is not None,
                "自动隐藏前检查是否正在播放（暂停时不隐藏）")
        c.check("showInspector" in restart,
                "自动隐藏前检查检查器是否打开")
        c.check("focus" in restart,
                "自动隐藏前检查按钮焦点（用户正在选按钮时不隐藏）")

    # 按钮态按键属于焦点引擎（返回走 onExitCommand、播放键走
    # onPlayPauseCommand），UIKit 入口必须整体让位，否则滞后窗口内
    # 播放键会被切两次、菜单键会直接退掉整个播放页。
    hk = body_of(src, "private func handleKey")
    c.check(bool(hk), "handleKey 存在")
    if hk:
        c.check(re.search(r"guard\s+level\s*!=\s*\.buttons\s+else\s*\{\s*return\s*\}",
                          hk) is not None,
                "handleKey 在按钮态整体让位（不与焦点引擎双触发）")

    # 焦点意外丢失时必须有兜底：退回进度条态，让锚点重新接住按键。
    #
    # 但**不能**一见到 nil 就退。进入按钮态的瞬间焦点必然短暂为 nil
    # （视频锚点已交权、按钮还没接住），旧实现立刻退回，表现就是
    # 「按一下下键，按钮行闪一下就消失」。真实反馈见 2026-11-18 用户回报。
    # 所以判据改成两段：
    #   1. onChange(of: focus) 里走的是「延迟复核」函数，不是直接改 level
    #   2. 该函数里才有 level = .progress，且带 sleep 宽限
    foc_cb = body_of(src, ".onChange(of: focus)")
    c.check(bool(foc_cb), "存在 onChange(of: focus) 分支")
    if foc_cb:
        c.check("scheduleFocusRecovery" in foc_cb,
                "焦点 nil 时走宽限复核（而不是立刻退回）",
                foc_cb.strip()[:80])
        c.check("level = .progress" not in foc_cb,
                "onChange(of: focus) 里不直接改 level（避免按钮行闪灭）")

    rec = body_of(src, "func scheduleFocusRecovery")
    c.check(bool(rec), "scheduleFocusRecovery 存在")
    if rec:
        c.check("Task.sleep" in rec,
                "退回前有宽限等待（给按钮接住焦点的时间）")
        c.check("level = .progress" in rec,
                "宽限到期且仍无焦点才退回进度条态")
        c.check(re.search(r"focus\s*==\s*nil", rec) is not None,
                "退回前复核焦点仍为 nil（期间恢复则放弃退回）")

    # 从按钮态回到锚点态时必须主动要焦点；进入按钮态时同样要请求，
    # 否则锚点已不可聚焦但焦点还挂在它身上，按钮的赋值会被弹回 nil。
    #
    # 判据必须钉在 updateUIView 里那一处：PlayerContainerView
    # 的 didMoveToWindow 里也有一次 setNeedsFocusUpdate（首次上屏用），
    # 只看"全文有没有这个词"的话，删掉 updateUIView 那处仍能命中
    # 另一处 —— 这就是变异测试 B10 报假 PASS 的原因。
    upd = body_of(src, "func updateUIView")
    c.check(bool(upd), "updateUIView 存在")
    if upd:
        c.check("setNeedsFocusUpdate" in upd,
                "焦点交接时主动请求焦点更新",
                upd.strip()[:80])
        c.check(re.search(r"capturesKeys\s*!=\s*wasCapturing", upd) is not None,
                "只在 capturesKeys 变化的那一帧要焦点（不是每帧都要）")
        c.check(re.search(r"if\s+capturesKeys\s*\{[\s\S]{0,160}?updateFocusIfNeeded",
                          upd) is not None,
                "回到锚点态时立即求值（保证方向键马上有落点）")

    # ─────────────────────────── 5. 点按手势不能抢按键
    print("\n== 5. 点按手势只允许在看片态启用 ==")
    c.check("UITapGestureRecognizer" in src,
            "用 UITapGestureRecognizer 提供点按（兼容 tvOS 15）")
    c.check("allowedPressTypes" in src, "手势限定 select 按键")
    c.check(re.search(r"tap\.isEnabled\s*=\s*tapEnabled", src) is not None,
            "手势启用状态跟随 tapEnabled")
    c.check(re.search(r"tapGesture\?\.isEnabled\s*=\s*tapEnabled", src) is not None,
            "updateUIView 同步手势启用状态")
    c.check("tapEnabled: level == .hidden" in src,
            "点按仅在看片态启用（其余两态已在播控界面里）")

    # capturesKeys 与 tapEnabled 必须分开：进度条态要收按键但不要点按
    c.check(re.search(r"capturesKeys:\s*level\s*!=\s*\.buttons", src) is not None,
            "按键捕获与点按是两件事（进度条态收按键但不点按）")

    # ─────────────────────────── 6. 焦点与按钮绑定
    print("\n== 6. 按钮焦点必须绑定同一 FocusState ==")

    btn = body_of(src, "struct CircleButton")
    c.check(bool(btn), "CircleButton 存在")
    if btn:
        c.check("FocusState<PlayerView.Control?>.Binding" in btn,
                "按钮接收父视图的 FocusState 绑定")
        c.check(re.search(r"\.focused\(focus,\s*equals:\s*tag\)", btn) is not None,
                "用 .focused(_:equals:) 绑定")
        # 按钮内部不能有任何可变状态。
        #
        # 早先只查 `"@FocusState" not in btn`，抓不住把焦点另存成
        # `@State var ownFocusFlag` 的写法（v2 的真实缺陷：焦点双真源，
        # 进过按钮就再也出不来）。子串 `@FocusState` 不含 `@State`，
        # 断言看着在守，实际一条都拦不住。这里逐个列出所有可变
        # 属性包裹器，只允许纯输入（let）与绑定（Binding）。
        holders = re.findall(
            r"@(State|FocusState|StateObject|ObservedObject|GestureState)\b", btn)
        c.check(not holders,
                "按钮内部不持有任何可变焦点状态（单一真源 = 父级 FocusState）",
                str(sorted(set(holders))))
        # 焦点读数只能来自传入的绑定，不能拼别的来源
        fields = re.findall(r"private var isFocused[^\n]*", btn)
        c.check(len(fields) == 1 and "focus.wrappedValue" in fields[0]
                and "||" not in fields[0] and "&&" not in fields[0],
                "isFocused 只由父级绑定决定（不叠加本地状态）",
                fields[0].strip()[:80] if fields else "未定义 isFocused")

    call_count = len(re.findall(r"CircleButton\(", src))
    bound = len(re.findall(r"focus:\s*\$focus", src))
    c.check(call_count > 0 and call_count == bound,
            f"{call_count} 个按钮全部传入 $focus", f"实际绑定 {bound}")

    c.check(".focusSection()" in src, "按钮行声明 focusSection")

    # ─────────────────────────── 7. 无残留死代码
    print("\n== 7. 无冲突残留 ==")
    for dead in ("ClickToToggleModifier", "ActivateToToggleModifier", "TapCatcher"):
        c.check(dead not in src, f"已移除旧手势方案 {dead}")

    # v3 的 showControls 布尔量必须彻底消失，否则和 level 双真源
    c.check(re.search(r"\bshowControls\b", src) is None,
            "不再存在 showControls 布尔量（与 level 双真源）")

    return c.report("播放器交互契约")


if __name__ == "__main__":
    sys.exit(main())