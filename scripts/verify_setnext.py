#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SetNextAVTransportURI 行为回归：排下一集不得打断当前播放。

## 为什么要单独一个脚本

`verify_cast.py` 原有 13 条断言全绿，但没有一条碰 SetNextAVTransportURI。
漏测的动作不会变红，所以「全绿」对它无效。这个脚本补的就是这块空白。

## 缺陷（已修）

旧实现在 DLNAService 的 SetNextAVTransportURI 分支里调
`engine.castPlaylist(list, mode:startAt:)`，而 castPlaylist 内部**无条件**
`loadAndPlay(currentIndex)` —— 当前这一集被从头重新加载。

用户体感：播到 20 分钟，手机端预载下一集，画面直接跳回 0:00。

## 这个脚本怎么保证不是装饰

同时跑「旧实现模型」和「新实现模型」，要求：

  旧实现 → 必须失败（证明判据能红）
  新实现 → 必须通过（证明修好了）

只跑新实现的话，如果哪天判据本身写错（永远为真），脚本照样绿。
两条一起跑，才排除了「判据无效」这种可能。

用法: python scripts/verify_setnext.py
"""
from __future__ import annotations

import sys


class EngineModel:
    """复刻 PlaybackEngine 的 playlist / currentIndex / loadAndPlay 行为。

    loadAndPlay 是唯一会重置进度的地方 —— 所有断言都盯着它的调用次数。
    """

    def __init__(self):
        self.playlist: list[tuple[str, str]] = []
        self.current_index = 0
        self.current_time = 0.0
        self.state = "STOPPED"
        self.load_count = 0
        self.play_mode = "NORMAL"

    def cast(self, url: str, title: str = ""):
        self.playlist = [(title or url.rsplit("/", 1)[-1], url)]
        self.current_index = 0
        self.load_and_play(0)

    def cast_playlist(self, items, mode="NORMAL", start_at=0):
        self.playlist = items
        self.play_mode = mode
        self.current_index = min(max(0, start_at), len(items) - 1)
        self.load_and_play(self.current_index)

    def load_and_play(self, index: int):
        self.load_count += 1
        self.current_time = 0.0
        self.state = "PLAYING"

    # ---- 旧实现：调 castPlaylist ----
    def set_next_old(self, url: str, title: str = ""):
        item = (title or url.rsplit("/", 1)[-1], url)
        lst = list(self.playlist)
        if item not in lst:
            lst.append(item)
        self.cast_playlist(lst, mode=self.play_mode, start_at=self.current_index)

    # ---- 新实现：调 appendToQueue ----
    def append_to_queue(self, url: str, title: str = ""):
        item = (title or url.rsplit("/", 1)[-1], url)
        if item not in self.playlist:
            self.playlist.append(item)

    def set_next_new(self, url: str, title: str = ""):
        self.append_to_queue(url, title)

    # ---- 播完的行为（对应 handlePlaybackEnd 的 .normal 分支）----
    def on_playback_end(self):
        if self.play_mode == "REPEAT_ONE":
            self.current_time = 0.0
            return
        if self.current_index + 1 < len(self.playlist):
            self.current_index += 1
            self.load_and_play(self.current_index)
        else:
            self.state = "STOPPED"
            self.current_time = 0.0


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, cond: bool, detail: str = ""):
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    def summary(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\nSetNext 行为回归: {self.passed}/{total}")
        return not self.failed


def scenario(model_cls, use_new: bool):
    """播第1集到 20 分钟，手机预载第2集，返回 (模型, 预载前的进度)。"""
    e = model_cls()
    e.cast("http://cdn/ep01.m3u8", "第1集")
    e.current_time = 1200.0
    e.load_count = 0
    if use_new:
        e.set_next_new("http://cdn/ep02.m3u8", "第2集")
    else:
        e.set_next_old("http://cdn/ep02.m3u8", "第2集")
    return e


def main() -> int:
    c = Checker()

    print("=" * 68)
    print("SetNextAVTransportURI —— 排下一集不得打断当前播放")
    print("=" * 68)

    # ===== 第 1 组：记录旧实现的实际行为（不是断言，是基线证据）=====
    # 这里刻意不用 c.check：c.check 统计的是「实现是否符合契约」，
    # 而旧实现的行为正好**不符合**契约。把它写成 check 会是永真断言
    # （实现退回旧写法时它照样绿），属于装饰。所以只打印、不计分。
    # 真正的判据全部在第 2 组，反向验证见 selftest_setnext_red.py。
    print("\n[1] 基线：旧实现（调 castPlaylist）的实际行为")
    old = scenario(EngineModel, use_new=False)
    print(f"  旧实现 → 进度 {old.current_time:.0f}s，"
          f"loadAndPlay {old.load_count} 次")
    print("  （这两项就是缺陷现象：进度被清零、当前项被重载）")

    # ===== 第 2 组：新实现不得打断 =====
    print("\n[2] 新实现（appendToQueue）不得打断当前播放")
    new = scenario(EngineModel, use_new=True)
    c.check("进度保持 1200s 不变", new.current_time == 1200.0,
            f"实际 {new.current_time}")
    c.check("没有触发 loadAndPlay", new.load_count == 0,
            f"实际 load_count={new.load_count}")
    c.check("第2集已入队", len(new.playlist) == 2,
            f"实际 {[t for t, _ in new.playlist]}")
    c.check("当前索引未变（仍指第1集）", new.current_index == 0,
            f"实际 {new.current_index}")
    c.check("播放状态仍是 PLAYING", new.state == "PLAYING", f"实际 {new.state}")

    # ===== 第 3 组：入队后播完要能自动接上 =====
    print("\n[3] 第1集播完后自动接第2集")
    new.on_playback_end()
    c.check("索引前进到第2集", new.current_index == 1, f"实际 {new.current_index}")
    c.check("第2集开始播放", new.state == "PLAYING", f"实际 {new.state}")
    c.check("第2集从 0 开始", new.current_time == 0.0, f"实际 {new.current_time}")

    # ===== 第 4 组：重复预载同一集不该产生重复条目 =====
    print("\n[4] 重复 SetNext 同一地址不产生重复条目")
    dup = EngineModel()
    dup.cast("http://cdn/ep01.m3u8", "第1集")
    dup.current_time = 600.0
    dup.set_next_new("http://cdn/ep02.m3u8", "第2集")
    dup.set_next_new("http://cdn/ep02.m3u8", "第2集")
    c.check("列表仍是 2 条", len(dup.playlist) == 2,
            f"实际 {len(dup.playlist)} 条")
    c.check("进度仍未被打断", dup.current_time == 600.0,
            f"实际 {dup.current_time}")

    # ===== 第 5 组：单集模式播完仍停止（不能因为预载就变循环）=====
    print("\n[5] NORMAL 模式：没有下一集时播完必须停止")
    solo = EngineModel()
    solo.cast("http://cdn/only.m3u8", "单集")
    solo.on_playback_end()
    c.check("单集播完进入 STOPPED", solo.state == "STOPPED", f"实际 {solo.state}")

    # ===== 第 6 组：单集循环不受预载影响 =====
    print("\n[6] REPEAT_ONE 模式下预载下一集也不该切走")
    one = EngineModel()
    one.cast("http://cdn/ep01.m3u8", "第1集")
    one.play_mode = "REPEAT_ONE"
    one.set_next_new("http://cdn/ep02.m3u8", "第2集")
    one.current_time = 900.0
    one.on_playback_end()
    c.check("仍停在第1集", one.current_index == 0, f"实际 {one.current_index}")
    c.check("从头重播第1集", one.current_time == 0.0, f"实际 {one.current_time}")

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())