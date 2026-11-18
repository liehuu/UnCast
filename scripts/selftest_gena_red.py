#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向验证 verify_gena.py 的断言真的能失败。

## 为什么

GENA 这块的断言大多是**静态文本匹配**（查 SCPD 里有没有 sendEvents="yes"、
查源码里有没有调用 notifyStateNow）。这类断言最容易写成装饰：

  - 正则匹配到了注释里的文字，代码删了照样绿
  - `is None` / `is not None` 组合写反，永远为真
  - 锚点打在相邻的无关函数里

全绿本身证明不了任何事。必须逐项注入缺陷，要求对应断言变红。

## 注入项

  G1  LastChange 改回 sendEvents="no"        → [A] 变红
  G2  删掉 LastChange 变量                    → [A] 变红
  G3  SUBSCRIBE 改回编造假 SID                → [B] 变红
  G4  删掉 onPlaybackFinished 调用            → [B] 变红
  G5  删掉 notifyIfStateChanged 的去重 guard  → [B] 变红
  G6  续订未知 SID 不返回 412                 → [B] 变红

用法: python scripts/selftest_gena_red.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_at(path):
    spec = importlib.util.spec_from_file_location("vg_variant", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_variant(name, mutate_upnp=None, mutate_svc=None, mutate_eng=None):
    """复制工程到沙箱，注入变异，跑 verify_gena，返回 (rc, 变红项)。"""
    tmp = tempfile.mkdtemp(prefix="uncast_gena_")
    try:
        for d in ("scripts", "UnCast/DLNA", "UnCast/Player"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)

        files = {
            "UnCast/DLNA/UPnPDescription.swift": mutate_upnp,
            "UnCast/DLNA/DLNAService.swift": mutate_svc,
            "UnCast/Player/PlaybackEngine.swift": mutate_eng,
        }
        for rel, mut in files.items():
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            if mut:
                src = mut(src)
            open(os.path.join(tmp, rel), "w", encoding="utf-8").write(src)

        # 复制被测脚本；它用 __file__ 定位 ../UnCast
        shutil.copy(os.path.join(HERE, "verify_gena.py"),
                    os.path.join(tmp, "scripts", "verify_gena.py"))

        m = load_at(os.path.join(tmp, "scripts", "verify_gena.py"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        reds = [ln.strip() for ln in out.splitlines() if "FAIL" in ln]
        return rc, reds
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 70)
    print("反向验证：verify_gena.py 的断言能失败吗")
    print("=" * 70)

    rc0, reds0 = run_variant("基线")
    print(f"\n[基线] 未注入 rc={rc0}，变红={len(reds0)}（期望 0 / 0）")

    cases = []

    # G1 LastChange 改回 sendEvents="no"
    def g1(src):
        return src.replace(
            '<stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>',
            '<stateVariable sendEvents="no"><name>LastChange</name><dataType>string</dataType></stateVariable>', 1)
    cases.append(("G1 LastChange 改 sendEvents=no", g1, None, None,
                  "LastChange 的 sendEvents=yes"))

    # G2 删掉 LastChange 变量
    def g2(src):
        return src.replace(
            '<stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>',
            "", 1)
    cases.append(("G2 删掉 LastChange 变量", g2, None, None,
                  "AVTransport 声明了 LastChange"))

    # G3 SUBSCRIBE 改回编造假 SID
    def g3(src):
        old = ('        case ("SUBSCRIBE", _):\n'
               '            return handleSubscribe(request)\n')
        new = ('        case ("SUBSCRIBE", _):\n'
               '            var r = HTTPServer.Response()\n'
               '            r.headers["SID"] = "uuid:\\(uuid)-event"\n'
               '            return r\n')
        return src.replace(old, new, 1)
    cases.append(("G3 SUBSCRIBE 改回假 SID", None, g3, None,
                  "SUBSCRIBE 走 handleSubscribe"))

    # G4 删掉 onPlaybackFinished 调用
    def g4(src):
        return src.replace("        onPlaybackFinished?()\n", "", 1)
    cases.append(("G4 删掉播完回调", None, None, g4,
                  "播完调用 onPlaybackFinished"))

    # G5 删掉去重 guard
    def g5(src):
        return src.replace(
            "        guard state != lastNotifiedState || uri != lastNotifiedURI else { return }\n",
            "", 1)
    cases.append(("G5 删掉状态去重", None, g5, None,
                  "状态去重"))

    # G6 续订未知 SID 不返回 412
    def g6(src):
        return src.replace("            bad.status = 412\n",
                           "            bad.status = 200\n", 1)
    cases.append(("G6 未知 SID 不回 412", None, g6, None,
                  "未知 SID 续订返回 412"))

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

    chk("基线全绿", rc0 == 0 and not reds0, f"rc={rc0}, 红={reds0}")

    print()
    for name, mu, ms, me, expect in cases:
        rc, reds = run_variant(name, mu, ms, me)
        chk(f"{name} → 退出码非 0", rc != 0, f"rc={rc}")
        chk(f"{name} → 命中「{expect}」", any(expect in r for r in reds),
            f"实际变红: {reds}")

    total = passed + len(failed)
    print(f"\n反向验证: {passed}/{total}")
    if failed:
        print("结论：断言存在装饰，需要收紧")
        return 1
    print("结论：断言有效（基线绿、六项注入全红且命中正确条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main())