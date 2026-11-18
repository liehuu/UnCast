#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连按累进 seek 的行为自测。

## 为什么要有这一层

静态断言只能证明「源码里有 seekSteps = [15,60,300]」，
证明不了**按下去实际跳到哪里**。而这部分逻辑最容易错的地方恰恰是
行为而非写法：

- 第二次按下去应该累计跳 60 秒，还是再跳 60 秒？
- 连按 4 次之后会停在 5 分钟吗？
- 换方向会不会沿用上一个方向的大档位？
- 起播位置在 30 秒处按快退，会不会跳到负数？

这些都得用模拟跑一遍。

## 做法

把 `seekOffset` 与 `seek()` 的状态机用 Python 逐行复刻，
然后按真实按键序列推演。复刻的每一处都对照 Swift 源码里的算法，
不是"另写一份差不多的"——复刻错了测试就没意义，
所以下面每条规则旁边都注明对应的 Swift 代码。

## 注意

这是**算法镜像**，不是对编译产物的测试。它约束的是"这套算法在
给定输入下输出什么"，不能替代真机验证手感（窗口 1 秒是否合适
只有上手才知道）。镜像的价值在于：档位/累加/夹紧这类确定性的
算术，人眼审代码很容易看漏，跑一遍就露。
"""
from __future__ import annotations

import sys

# ── 与 Swift 侧对应的常量 ────────────────────────────────────
# PlayerView.seekSteps
SEEK_STEPS = [15.0, 60.0, 300.0]
# PlayerView.seekTailStep
SEEK_TAIL_STEP = 300.0


def seek_offset(streak: int) -> float:
    """对应 PlayerView.seekOffset(forStreak:)。

    Swift:
        guard streak > 0 else { return seekSteps[0] }
        if streak <= seekSteps.count { return seekSteps[streak - 1] }
        let extra = Double(streak - seekSteps.count) * seekTailStep
        return seekSteps[seekSteps.count - 1] + extra
    """
    if streak <= 0:
        return SEEK_STEPS[0]
    if streak <= len(SEEK_STEPS):
        return SEEK_STEPS[streak - 1]
    extra = float(streak - len(SEEK_STEPS)) * SEEK_TAIL_STEP
    return SEEK_STEPS[-1] + extra


class Seeker:
    """对应 PlayerView 的 seek 状态机（seekSign / seekStreak / seekAnchor）。"""

    def __init__(self, position: float, duration: float):
        self.position = position          # 相当于 engine.currentTime
        self.duration = duration
        self.anchor: float | None = None
        self.streak = 0
        self.sign = 0

    def expire(self):
        """对应 armSeekStreakTimeout 超时后的复位。"""
        self.streak = 0
        self.sign = 0
        self.anchor = None

    def seek(self, sign: int) -> float:
        """按一次键，返回本次累计跳转的带符号秒数。"""
        d = 1 if sign >= 0 else -1
        continuing = (self.sign == d) and (self.streak > 0) and (self.anchor is not None)
        if not continuing:
            self.anchor = self.position
            self.streak = 0
        self.sign = d
        self.streak += 1

        magnitude = seek_offset(self.streak)
        delta = magnitude * d
        anchor = self.anchor if self.anchor is not None else self.position
        max_time = self.duration if self.duration > 0 else anchor + magnitude
        target = min(max(0.0, anchor + delta), max_time)

        # 模拟 seek 落地：真机上这是异步的，落地点成为下一次的 currentTime。
        # 注意 anchor 不变 —— 这正是"绝对定位"的关键。
        self.position = target
        return delta


class Ctx:
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


def main() -> int:
    c = Ctx()

    print("== 1. 档位表：按一次 15s / 两次 1min / 三次 5min ==")
    c.check(seek_offset(1) == 15, "第 1 次累计 15 秒", str(seek_offset(1)))
    c.check(seek_offset(2) == 60, "第 2 次累计 60 秒", str(seek_offset(2)))
    c.check(seek_offset(3) == 300, "第 3 次累计 300 秒", str(seek_offset(3)))

    print("\n== 2. 第 3 次之后必须继续递进（不能停住）==")
    c.check(seek_offset(4) == 600, "第 4 次累计 10 分钟", str(seek_offset(4)))
    c.check(seek_offset(5) == 900, "第 5 次累计 15 分钟", str(seek_offset(5)))
    # 关键：单调递增，绝不回退
    seq = [seek_offset(i) for i in range(1, 9)]
    c.check(all(b > a for a, b in zip(seq, seq[1:])),
            "档位严格单调递增（不会出现按更多次反而跳更少）", str(seq))

    print("\n== 3. 连按的实际落点 ==")
    s = Seeker(position=100, duration=3600)
    s.seek(1)
    c.check(s.position == 115, "起点 100s 按一次快进 → 115s", str(s.position))
    s.seek(1)
    c.check(s.position == 160, "再按一次 → 累计 60s，落在 160s", str(s.position))
    s.seek(1)
    c.check(s.position == 400, "再按一次 → 累计 300s，落在 400s", str(s.position))
    s.seek(1)
    c.check(s.position == 700, "第 4 次 → 累计 600s，落在 700s", str(s.position))

    print("\n== 4. 绝对定位：异步 seek 未落地也不能少跳 ==")
    # 模拟"上一次 seek 还没落地，currentTime 仍是起点"
    s2 = Seeker(position=100, duration=3600)
    s2.seek(1)                  # 落点 115
    stale = 100                 # 真机上此刻 currentTime 可能还是 100
    saved = s2.position
    s2.position = stale         # 故意制造"未落地"
    s2.seek(1)                  # 第二次应按 anchor(100)+60 = 160
    expected = 160
    c.check(s2.position == expected,
            "currentTime 停留在旧值时仍按起点+累计计算", 
            f"实际 {s2.position}，期望 {expected}")
    s2.position = saved

    print("\n== 5. 换方向必须重新起算 ==")
    s3 = Seeker(position=1000, duration=3600)
    s3.seek(1); s3.seek(1); s3.seek(1)   # 快进到 +300
    pos_after_fwd = s3.position
    s3.seek(-1)                           # 立刻反向，应只退 15 秒
    c.check(s3.position == pos_after_fwd - 15,
            "快进三次后立刻快退 → 只退 15 秒（不沿用 300 档）",
            f"实际 {s3.position}，期望 {pos_after_fwd - 15}")

    print("\n== 6. 停顿超窗口后重新起算 ==")
    s4 = Seeker(position=1000, duration=3600)
    s4.seek(1); s4.seek(1); s4.seek(1)
    pos = s4.position
    s4.expire()                           # 窗口超时
    s4.seek(1)
    c.check(s4.position == pos + 15,
            "超时后再按 → 从 15 秒重新起算",
            f"实际 {s4.position}，期望 {pos + 15}")

    print("\n== 7. 边界夹紧 ==")
    s5 = Seeker(position=10, duration=3600)
    s5.seek(-1)
    c.check(s5.position == 0, "起播 10s 处快退 → 夹到 0（不出现负数）",
            str(s5.position))

    s6 = Seeker(position=3590, duration=3600)
    s6.seek(1); s6.seek(1); s6.seek(1)
    c.check(s6.position == 3600, "接近片尾快进 → 夹到 duration（不越界）",
            str(s6.position))

    # 时长为 0（还没读到元数据）时不能算出 0 长度把位置压成 0
    s7 = Seeker(position=50, duration=0)
    s7.seek(1)
    c.check(s7.position == 65, "duration 未知（0）时按 15 秒照常前进",
            str(s7.position))

    print("\n== 8. 快退连按同样累进 ==")
    s8 = Seeker(position=3600, duration=3600)
    s8.seek(-1)
    c.check(s8.position == 3585, "快退一次 → -15s", str(s8.position))
    s8.seek(-1)
    c.check(s8.position == 3540, "快退两次 → 累计 -60s", str(s8.position))
    s8.seek(-1)
    c.check(s8.position == 3300, "快退三次 → 累计 -300s", str(s8.position))

    total = c.passes + len(c.fails)
    print(f"\n连按累进行为自测: {c.passes}/{total}")
    if c.fails:
        print("失败项:")
        for f in c.fails:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())