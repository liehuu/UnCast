#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向验证 verify_cast.py 用例 11（源码接线断言）真的能失败。

## 为什么

用例 11 是七条**静态**断言，查的是 Swift 源码里的调用形态。
静态断言最容易退化成装饰：正则写歪、锚点打在注释上、或者根本匹配不到
却因为 `is None` / `in` 的组合碰巧通过。全绿不等于有效。

这里把源码复制到临时目录，逐项注入缺陷后重跑，要求对应断言变红。

## 注入项

  S1  把 appendToQueue 换回 castPlaylist      → 「不再调用 castPlaylist」应变红
  S2  删掉 appendToQueue 调用                  → 「调用 appendToQueue」应变红
  S3  appendToQueue 参数标签改掉               → 「首参是 url:」应变红
  S4  appendToQueue 内部加 loadAndPlay         → 「不调用 loadAndPlay」应变红
  S5  appendToQueue 内部改 currentIndex        → 「不修改 currentIndex」应变红

用法: python scripts/selftest_cast_red.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_cast", os.path.join(HERE, "verify_cast.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_case(name: str, mutate_svc=None, mutate_eng=None) -> tuple[int, list[str]]:
    """在临时沙箱里复制工程，注入变异，跑用例 11，返回 (退出码, 失败项)。"""
    tmp = tempfile.mkdtemp(prefix="uncast_cast_")
    try:
        # 只复制用例 11 需要的两个 Swift 文件，保持相对路径结构
        dst_scripts = os.path.join(tmp, "scripts")
        dst_dlna = os.path.join(tmp, "UnCast", "DLNA")
        dst_player = os.path.join(tmp, "UnCast", "Player")
        for d in (dst_scripts, dst_dlna, dst_player):
            os.makedirs(d, exist_ok=True)

        svc_src = open(os.path.join(ROOT, "UnCast", "DLNA",
                                    "DLNAService.swift"), encoding="utf-8").read()
        eng_src = open(os.path.join(ROOT, "UnCast", "Player",
                                    "PlaybackEngine.swift"), encoding="utf-8").read()

        if mutate_svc:
            svc_src = mutate_svc(svc_src)
        if mutate_eng:
            eng_src = mutate_eng(eng_src)

        open(os.path.join(dst_dlna, "DLNAService.swift"), "w",
             encoding="utf-8").write(svc_src)
        open(os.path.join(dst_player, "PlaybackEngine.swift"), "w",
             encoding="utf-8").write(eng_src)
        # 用例 11 用 __file__ 定位 ../UnCast/...，把脚本也放进去
        shutil.copy(os.path.join(HERE, "verify_cast.py"),
                    os.path.join(dst_scripts, "verify_cast.py"))

        m = load_module_path(os.path.join(dst_scripts, "verify_cast.py"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        reds = [ln.strip() for ln in out.splitlines() if "❌" in ln]
        return rc, reds
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_module_path(path: str):
    spec = importlib.util.spec_from_file_location("vc_variant", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=" * 70)
    print("反向验证：verify_cast.py 用例 11 的七条静态断言能失败吗")
    print("=" * 70)

    # 基线
    rc0, reds0 = run_case("基线")
    print(f"\n[基线] 未注入退出码={rc0}，失败项={len(reds0)}（期望 0 / 0）")

    cases = []

    # S1：把 appendToQueue 调用换回 castPlaylist
    def s1(src: str) -> str:
        return src.replace(
            "engine.appendToQueue(url: url, title: title)",
            "engine.castPlaylist(engine.playlist, mode: engine.playMode, "
            "startAt: engine.currentIndex)")
    cases.append(("S1 换回 castPlaylist", s1, None,
                  "不再调用 engine.castPlaylist"))

    # S2：删掉 appendToQueue 调用
    def s2(src: str) -> str:
        return src.replace(
            "engine.appendToQueue(url: url, title: title)",
            "// 注入：什么都不做")
    cases.append(("S2 删掉入队调用", s2, None, "调用 engine.appendToQueue"))

    # S3：参数标签改掉
    def s3(src: str) -> str:
        return src.replace(
            "engine.appendToQueue(url: url, title: title)",
            "engine.appendToQueue(link: url, name: title)")
    cases.append(("S3 参数标签改掉", s3, None, "appendToQueue 首参是 url:"))

    # S4：appendToQueue 内部加 loadAndPlay
    def s4(src: str) -> str:
        return src.replace(
            "        guard !playlist.contains(item) else { return }\n"
            "        playlist.append(item)",
            "        guard !playlist.contains(item) else { return }\n"
            "        playlist.append(item)\n"
            "        loadAndPlay(index: currentIndex)")
    cases.append(("S4 内部加 loadAndPlay", None, s4, "appendToQueue 内不调用 loadAndPlay"))

    # S5：appendToQueue 内部改 currentIndex
    def s5(src: str) -> str:
        return src.replace(
            "        guard !playlist.contains(item) else { return }\n"
            "        playlist.append(item)",
            "        guard !playlist.contains(item) else { return }\n"
            "        playlist.append(item)\n"
            "        currentIndex = playlist.count - 1")
    cases.append(("S5 内部改 currentIndex", None, s5, "appendToQueue 不修改 currentIndex"))

    passed = 0
    failed = []

    def chk(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    chk("基线全绿（用例 11 七条都过）", rc0 == 0 and not reds0,
        f"rc={rc0}, 变红={reds0}")

    print()
    for name, ms, me, expect in cases:
        rc, reds = run_case(name, ms, me)
        hit = any(expect in r for r in reds)
        chk(f"{name} → 变红 {len(reds)} 条", rc != 0, f"rc={rc}")
        chk(f"{name} → 命中断言「{expect}」", hit,
            f"实际变红: {reds}")

    total = passed + len(failed)
    print(f"\n反向验证: {passed}/{total}")
    if failed:
        print("结论：有用例抓不住，静态断言存在装饰")
        return 1
    print("结论：七条静态断言均有效（基线绿、注入红、且红了正确的那条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())