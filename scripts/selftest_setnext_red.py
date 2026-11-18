#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向验证 verify_setnext.py 的判据真的能失败。

## 为什么必须有这个脚本

`verify_setnext.py` 全绿本身说明不了任何事 —— 只要判据写错（永远为真），
它照样全绿。本脚本把被测模型逐项改回缺陷形态，要求对应断言变红。

## 做法

把 EngineModel.set_next_new 换成旧实现（调 castPlaylist），
重跑 test 里的第 2 / 4 组，要求：

  - 退出码从 0 变成 1
  - 至少 3 条断言变红
  - 且变红的必须包含「进度」和「loadAndPlay」两条关键判据

三条都要满足。只看退出码的话，如果判据坏掉了但别的地方碰巧变红，
会被误判成有效。

用法: python scripts/selftest_setnext_red.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    path = os.path.join(HERE, "verify_setnext.py")
    spec = importlib.util.spec_from_file_location("verify_setnext", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=" * 68)
    print("反向验证：把新实现改回旧实现，verify_setnext 必须变红")
    print("=" * 68)

    m = load_module()

    # 基线：正常实现必须绿
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc_ok = m.main()
    print(f"\n[基线] 正常实现退出码 = {rc_ok}（期望 0）")

    # 注入：换回旧实现
    def broken_set_next(self, url, title=""):
        item = (title or url.rsplit("/", 1)[-1], url)
        lst = list(self.playlist)
        if item not in lst:
            lst.append(item)
        self.cast_playlist(lst, mode=self.play_mode,
                           start_at=self.current_index)

    orig = m.EngineModel.set_next_new
    m.EngineModel.set_next_new = broken_set_next
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_bad = m.main()
        out = buf.getvalue()
    finally:
        m.EngineModel.set_next_new = orig

    reds = [ln.strip() for ln in out.splitlines() if "FAIL" in ln]
    print(f"\n[注入] 旧实现退出码 = {rc_bad}（期望 1）")
    print(f"[注入] 变红 {len(reds)} 条：")
    for r in reds:
        print(f"   {r}")

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

    print("\n判据判定：")
    chk("基线（正常实现）返回 0", rc_ok == 0, f"实际 {rc_ok}")
    chk("注入旧实现后返回 1", rc_bad == 1, f"实际 {rc_bad}")
    chk("至少 3 条断言变红", len(reds) >= 3, f"实际 {len(reds)} 条")
    chk("「进度保持」这条变红了",
        any("进度保持" in r for r in reds), f"变红的: {reds}")
    chk("「没有触发 loadAndPlay」这条变红了",
        any("loadAndPlay" in r for r in reds), f"变红的: {reds}")
    chk("「重复 SetNext 不打断」这条变红了",
        any("重复" in r or "列表仍是" in r or "进度仍未" in r for r in reds),
        f"变红的: {reds}")

    total = passed + len(failed)
    print(f"\n反向验证: {passed}/{total}")
    if failed:
        print("结论：判据可被绕过，需要收紧")
        return 1
    print("结论：判据有效（能绿能红，且红了正确的那几条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())