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
  G7  takeTargets 去掉 service 过滤           → [B2] 变红
  G8  notify 去掉 service 参数（退回广播）    → [B2] 变红
  G9  音量事件发到 AVTransport（串服务）      → [B2] 变红
  G10 Subscriber 去掉 service 字段            → [B2] 变红
  G11 分流判断失效但保留字段（_ = service）    → [B2] 变红

G11 单独存在的原因：只删「字段」或只删「过滤」时，另一处还能让断言变红。
但把过滤条件换成 `_ = service`（参数还在、字段还在、就是不判断）时，
前两类注入都抓不到 —— 这类「看起来对、实际不判断」的变异必须单独覆盖。

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


def run_variant(name, mutate_upnp=None, mutate_svc=None, mutate_eng=None,
                mutate_ev=None):
    """复制工程到沙箱，注入变异，跑 verify_gena，返回 (rc, 变红项)。"""
    tmp = tempfile.mkdtemp(prefix="uncast_gena_")
    try:
        for d in ("scripts", "UnCast/DLNA", "UnCast/Player"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)

        files = {
            "UnCast/DLNA/UPnPDescription.swift": mutate_upnp,
            "UnCast/DLNA/DLNAService.swift": mutate_svc,
            "UnCast/Player/PlaybackEngine.swift": mutate_eng,
            "UnCast/DLNA/EventSubscription.swift": mutate_ev,
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
    cases.append(("G1 LastChange 改 sendEvents=no", g1, None, None, None,
                  "LastChange 的 sendEvents=yes"))

    # G2 删掉 LastChange 变量
    def g2(src):
        return src.replace(
            '<stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>',
            "", 1)
    cases.append(("G2 删掉 LastChange 变量", g2, None, None, None,
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
    cases.append(("G3 SUBSCRIBE 改回假 SID", None, g3, None, None,
                  "SUBSCRIBE 走 handleSubscribe"))

    # G4 删掉 onPlaybackFinished 调用
    def g4(src):
        return src.replace("        onPlaybackFinished?()\n", "", 1)
    cases.append(("G4 删掉播完回调", None, None, g4, None,
                  "播完调用 onPlaybackFinished"))

    # G5 删掉去重 guard
    def g5(src):
        return src.replace(
            "        guard state != lastNotifiedState || uri != lastNotifiedURI else { return }\n",
            "", 1)
    cases.append(("G5 删掉状态去重", None, g5, None, None,
                  "状态去重"))

    # G6 续订未知 SID 不返回 412
    def g6(src):
        return src.replace("            bad.status = 412\n",
                           "            bad.status = 200\n", 1)
    cases.append(("G6 未知 SID 不回 412", None, g6, None, None,
                  "未知 SID 续订返回 412"))

    # G7 takeTargets 去掉 service 过滤 —— 退回「谁订都给」的广播行为
    def g7(src):
        return src.replace(
            "            if let service, sub.service != service { continue }\n",
            "", 1)
    cases.append(("G7 takeTargets 去掉服务过滤", None, None, None,
                  ("ev", g7), "takeTargets 按 service 过滤"))

    # G8 notify 去掉 service 参数
    def g8(src):
        old = ("    func notify(body: String, service: String? = nil,\n"
               "                log: @escaping (String) -> Void = { _ in }) {\n"
               "        let targets = takeTargets(service: service)\n")
        new = ("    func notify(body: String,\n"
               "                log: @escaping (String) -> Void = { _ in }) {\n"
               "        let targets = takeTargets(service: nil)\n")
        return src.replace(old, new, 1)
    cases.append(("G8 notify 去掉 service 参数", None, None, None,
                  ("ev", g8), "notify 接受 service 参数"))

    # G9 音量事件发到 AVTransport（串服务）
    def g9(src):
        return src.replace(
            'events.notify(body: volumeBody, service: "RenderingControl", log: log)',
            'events.notify(body: volumeBody, service: "AVTransport", log: log)', 1)
    cases.append(("G9 音量事件串到 AVTransport", None, g9, None,
                  None, "音量事件发往 RenderingControl"))

    # G10 Subscriber 去掉 service 字段
    def g10(src):
        return src.replace("        let service: String\n", "", 1)
    cases.append(("G10 Subscriber 去掉 service 字段", None, None, None,
                  ("ev", g10), "Subscriber 有 service 字段"))

    # G11 分流判断失效但保留字段（用 _ = service 吃掉参数）
    def g11(src):
        old = ("        for (sid, var sub) in subscribers {\n"
               "            if let service, sub.service != service { continue }\n")
        new = ("        for (sid, var sub) in subscribers {\n"
               "            _ = service\n")
        return src.replace(old, new, 1)
    cases.append(("G11 分流判断失效但保留字段", None, None, None,
                  ("ev", g11), "takeTargets 按 service 过滤"))

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
    for name, mu, ms, me, extra, expect in cases:
        # extra 是 (目标, 变异函数)，目标只会是 "ev"（EventSubscription）
        # —— 见 run_variant 里 mutate_ev 的位置参数。
        mev = extra[1] if extra else None
        rc, reds = run_variant(name, mu, ms, me, mev)
        chk(f"{name} → 退出码非 0", rc != 0, f"rc={rc}")
        chk(f"{name} → 命中「{expect}」", any(expect in r for r in reds),
            f"实际变红: {reds}")

    total = passed + len(failed)
    print(f"\n反向验证: {passed}/{total}")
    if failed:
        print("结论：断言存在装饰，需要收紧")
        return 1
    print(f"结论：断言有效（基线绿、{len(cases)} 项注入全红且命中正确条目）")
    return 0


if __name__ == "__main__":
    sys.exit(main())