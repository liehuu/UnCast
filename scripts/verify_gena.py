#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GENA 事件机制契约：手机端能不能收到「播完了」。

## 为什么这块必须单独验证

自动连播不是接收端自己能决定的。手机 App 的流程是：

    投第 1 集 → 订阅我们的事件 → 收到 TransportState=STOPPED
    → 判断「这集完了」→ 发 SetAVTransportURI(第 2 集)

中间任何一环断掉，用户看到的现象都一样：**播完停住，得回手机重新投**。
而这条链路每一环都不会报错、不会崩、编译全过。

旧实现三处同时断：
  1. SCPD 里所有 stateVariable 都是 sendEvents="no"，也没有 LastChange
     → 等于对外声明「本设备不产生任何事件」，严格客户端直接不订阅
  2. SUBSCRIBE 只回一个编造的 SID，从不真发 NOTIFY
  3. 播完（AVPlayerItemDidPlayToEndTime）时没有任何对外通知

## 断言分三层

  A. 源码声明层 —— SCPD 必须宣告 LastChange + sendEvents="yes"
  B. 源码接线层 —— SUBSCRIBE 真建订阅、播完真触发通知
  C. 协议行为层 —— 复刻 EventSubscription 逻辑，跑真实订阅/续订/退订/事件体

用法: python scripts/verify_gena.py
"""
from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ============================================================
# C. 协议行为模型（复刻 EventSubscription.swift）
# ============================================================

class SubModel:
    """按 EventSubscription.swift 的行为建模。"""

    TIMEOUT = 1800

    def __init__(self):
        self.subs: dict[str, dict] = {}

    @staticmethod
    def parse_callback(raw):
        if not raw:
            return None
        cand = raw
        lt = raw.find("<")
        if lt >= 0:
            gt = raw.find(">", lt)
            if gt > lt:
                cand = raw[lt + 1:gt]
        cand = cand.strip()
        if not cand.lower().startswith("http"):
            return None
        if "://" not in cand:
            return None
        host = cand.split("://", 1)[1].split("/")[0]
        if not host:
            return None
        return cand

    @staticmethod
    def parse_timeout(raw):
        if not raw:
            return SubModel.TIMEOUT
        low = raw.lower()
        if "infinite" in low:
            return SubModel.TIMEOUT
        i = low.find("second-")
        if i >= 0:
            num = ""
            for ch in low[i + 7:]:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                return min(int(num), SubModel.TIMEOUT)
        return SubModel.TIMEOUT

    def subscribe(self, callback, timeout=None, service="AVTransport"):
        sid = "uuid:test-" + str(len(self.subs) + 1)
        self.subs[sid] = {
            "callback": callback,
            "service": service,
            "expires": datetime.now() + timedelta(
                seconds=timeout or self.TIMEOUT),
            "seq": 0,
        }
        return sid

    def renew(self, sid, timeout=None):
        if sid not in self.subs:
            return False
        self.subs[sid]["expires"] = datetime.now() + timedelta(
            seconds=timeout or self.TIMEOUT)
        self.subs[sid]["seq"] = 0
        return True

    def unsubscribe(self, sid):
        return self.subs.pop(sid, None) is not None

    def take_targets(self, service=None):
        now = datetime.now()
        self.subs = {k: v for k, v in self.subs.items() if v["expires"] > now}
        out = []
        for sid, v in self.subs.items():
            # service=None 表示不筛（调试用）；给定值时只取订了该服务的
            if service is not None and v.get("service") != service:
                continue
            v["seq"] += 1
            out.append((sid, v["callback"], v["seq"]))
        return out

    @staticmethod
    def service_name_from_event_url(path):
        """按 DLNAService.serviceName(fromEventURL:) 建模。"""
        name = path.replace("/upnp/event/", "")
        # 只剥结尾数字（AVTransport1 → AVTransport），不剥别的
        while name and name[-1].isdigit():
            name = name[:-1]
        if name in ("AVTransport", "RenderingControl", "ConnectionManager"):
            return name
        return "AVTransport"


def escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;")
             .replace("'", "&apos;"))


def last_change_body(state, uri="", dur="00:00:00", mode="NORMAL"):
    inner = (
        '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
        '<InstanceID val="0">'
        f'<TransportState val="{escape(state)}"/>'
        '<TransportStatus val="OK"/>'
        f'<CurrentTrackURI val="{escape(uri)}"/>'
        f'<CurrentTrackDuration val="{escape(dur)}"/>'
        f'<CurrentPlayMode val="{escape(mode)}"/>'
        "</InstanceID></Event>"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
        f'<e:property><LastChange>{escape(inner)}</LastChange></e:property>'
        "</e:propertyset>"
    )


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    def summary(self):
        total = self.passed + len(self.failed)
        print(f"\nGENA 事件机制: {self.passed}/{total}")
        return not self.failed


def read(rel):
    p = os.path.join(ROOT, rel)
    return open(p, encoding="utf-8").read()


def main() -> int:
    c = Checker()

    print("=" * 70)
    print("GENA 事件机制 —— 自动连播的关键一环")
    print("=" * 70)

    # ---------- A. SCPD 声明 ----------
    print("\n[A] SCPD 是否宣告了可订阅事件")
    upnp = read("UnCast/DLNA/UPnPDescription.swift")

    # 切出 AVTransport 的 scpd（第一个 <scpd> 块）
    m = re.search(r"static let avTransportSCPD.*?\"\"\"(.*?)\"\"\"", upnp, re.S)
    avt = m.group(1) if m else ""
    c.check("找到 AVTransport SCPD", bool(avt), "正则未匹配")

    c.check("AVTransport 声明了 LastChange 变量",
            re.search(r'<name>\s*LastChange\s*</name>', avt) is not None,
            "没有 LastChange")

    # sendEvents="yes" 必须出现在 LastChange 那一行附近
    lc_yes = re.search(
        r'<stateVariable\s+sendEvents="yes"\s*>\s*<name>\s*LastChange\s*</name>',
        avt)
    c.check("LastChange 的 sendEvents=yes", lc_yes is not None,
            "LastChange 不是 sendEvents=yes —— 客户端不会订阅")

    # 反向：TransportState 本身不该 sendEvents=yes（它走 LastChange 派发）
    ts_yes = re.search(
        r'<stateVariable\s+sendEvents="yes"\s*>\s*<name>\s*TransportState',
        avt)
    c.check("TransportState 保持 sendEvents=no（走 LastChange）",
            ts_yes is None, "TransportState 不该单独 sendEvents=yes")

    # RenderingControl 也要有 LastChange
    m2 = re.search(r"static let renderingControlSCPD.*?\"\"\"(.*?)\"\"\"", upnp, re.S)
    rcs = m2.group(1) if m2 else ""
    c.check("RenderingControl 也声明 LastChange(sendEvents=yes)",
            re.search(r'<stateVariable\s+sendEvents="yes"\s*>\s*<name>\s*LastChange',
                      rcs) is not None,
            "RenderingControl 缺 LastChange")

    # ---------- B. 源码接线 ----------
    print("\n[B] 源码接线：SUBSCRIBE 真建订阅、播完真发通知")
    svc = read("UnCast/DLNA/DLNAService.swift")

    c.check("SUBSCRIBE 走 handleSubscribe（不再是假 SID）",
            re.search(r'case\s*\("SUBSCRIBE",\s*_\)\s*:\s*\n\s*return\s+handleSubscribe',
                      svc) is not None,
            "SUBSCRIBE 仍是旧写法")
    c.check("UNSUBSCRIBE 走 handleUnsubscribe",
            re.search(r'case\s*\("UNSUBSCRIBE",\s*_\)\s*:\s*\n\s*return\s+handleUnsubscribe',
                      svc) is not None,
            "UNSUBSCRIBE 仍是旧写法")

    # 旧写法：编造 SID
    c.check("不再有编造的假 SID",
            re.search(r'headers\["SID"\]\s*=\s*"uuid:\\\(uuid\)-event"', svc) is None,
            "还在返回编造的 SID")

    # 未知 SID 续订要回 412（规范行为）
    sub_fn = ""
    mm = re.search(r"private func handleSubscribe.*?\n    \}", svc, re.S)
    if mm:
        sub_fn = mm.group(0)
    c.check("找到 handleSubscribe 实现", bool(sub_fn), "正则未匹配")
    c.check("未知 SID 续订返回 412",
            re.search(r"status\s*=\s*412", sub_fn) is not None,
            "没回 412")
    c.check("新订阅时推初始事件（客户端要基线）",
            "sendStateEvent" in sub_fn,
            "订阅后不推初始状态")

    # 播完必须触发通知
    eng = read("UnCast/Player/PlaybackEngine.swift")
    end_fn = ""
    mm = re.search(r"private func handlePlaybackEnd\(\).*?\n    \}", eng, re.S)
    if mm:
        end_fn = mm.group(0)
    c.check("找到 handlePlaybackEnd", bool(end_fn), "正则未匹配")
    c.check("播完调用 onPlaybackFinished 回调",
            re.search(r"onPlaybackFinished\?", end_fn) is not None,
            "播完没有对外通知 —— 手机端永远不知道这集结束了")

    c.check("DLNAService 接上了 onPlaybackFinished",
            re.search(r"engine\.onPlaybackFinished\s*=", svc) is not None,
            "没接回调")
    c.check("回调里调 notifyStateNow",
            re.search(r"engine\.onPlaybackFinished\s*=.*?notifyStateNow", svc, re.S)
            is not None,
            "回调没发事件")

    # 去重：不能每秒刷 NOTIFY
    notify_fn = ""
    mm = re.search(r"func notifyIfStateChanged\(\).*?\n    \}", svc, re.S)
    if mm:
        notify_fn = mm.group(0)
    c.check("状态去重（值没变不发）",
            re.search(r"guard\s+state\s*!=\s*lastNotifiedState", notify_fn) is not None,
            "没有去重，会把手机端淹掉")

    # ---------- B2. 事件按服务分流 ----------
    #
    # 为什么必须单独测这一层：AVTransport 与 RenderingControl 各有自己的
    # eventSubURL，客户端是**分开订阅**的。把 Volume 事件发给 AVTransport
    # 的订阅者，严格客户端解析 LastChange 时找不到它要的 TransportState，
    # 会当成格式错误。三个现象都可能出现（丢掉事件 / 报错 / 直接退订），
    # 但都不会崩、不会编译失败，只有真连播时才暴露。
    print("\n[B2] 事件按服务分流（AVTransport / RenderingControl）")
    ev = read("UnCast/DLNA/EventSubscription.swift")

    c.check("Subscriber 有 service 字段",
            re.search(r"let\s+service\s*:\s*String", ev) is not None,
            "Subscriber 没记服务名，无法分流")
    c.check("notify 接受 service 参数",
            re.search(r"func\s+notify\s*\([^)]*service\s*:\s*String\?", ev, re.S)
            is not None,
            "notify 没有 service 参数，只能广播")
    c.check("takeTargets 按 service 过滤",
            re.search(r"sub\.service\s*!=\s*service", ev) is not None,
            "takeTargets 不按服务过滤 —— Volume 事件会发给 AVTransport 订阅者")

    # 调用侧：AVTransport 与 RenderingControl 必须各自发，不能合成一次广播
    send_fn = ""
    mm = re.search(r"private func sendStateEvent\(.*?\n    \}", svc, re.S)
    if mm:
        send_fn = mm.group(0)
    c.check("找到 sendStateEvent 实现", bool(send_fn), "正则未匹配")
    c.check("状态事件发往 AVTransport 服务",
            re.search(r'notify\(body:\s*body,\s*service:\s*"AVTransport"', send_fn)
            is not None,
            "状态事件没指定 AVTransport")
    c.check("音量事件发往 RenderingControl 服务",
            re.search(r'notify\(body:\s*volumeBody,\s*service:\s*"RenderingControl"',
                      send_fn) is not None,
            "音量事件没指定 RenderingControl")

    # 订阅侧要按 eventSubURL 解析服务名
    c.check("subscribe 传入从 URL 解析出的服务名",
            re.search(r"events\.subscribe\([^)]*service:\s*service", svc, re.S)
            is not None,
            "订阅时没记录服务名")
    c.check("serviceName 从 /upnp/event/ 路径解析",
            re.search(r"func\s+serviceName\s*\(fromEventURL", svc) is not None,
            "缺 serviceName(fromEventURL:)")

    # ---------- C. 行为 ----------
    print("\n[C] 协议行为：订阅生命周期与事件体")
    s = SubModel()

    # CALLBACK 解析
    for raw, want in [
        ("<http://192.168.1.5:1234/notify>", "http://192.168.1.5:1234/notify"),
        ("http://192.168.1.5:1234/notify", "http://192.168.1.5:1234/notify"),
    ]:
        c.check(f"CALLBACK 解析 {raw[:28]}", SubModel.parse_callback(raw) == want,
                f"实际 {SubModel.parse_callback(raw)}")
    c.check("非法 CALLBACK 返回 None",
            SubModel.parse_callback("ftp://x/y") is None)
    c.check("空 CALLBACK 返回 None", SubModel.parse_callback(None) is None)

    # TIMEOUT 解析
    c.check("Second-300 → 300", SubModel.parse_timeout("Second-300") == 300)
    c.check("Second-infinite → 1800 上限",
            SubModel.parse_timeout("Second-infinite") == 1800)
    c.check("超上限被夹到 1800",
            SubModel.parse_timeout("Second-99999") == 1800)
    c.check("缺 TIMEOUT 用默认 1800",
            SubModel.parse_timeout(None) == 1800)

    # 订阅/续订/退订
    sid = s.subscribe("http://192.168.1.5:1234/notify")
    c.check("新订阅产生 SID", sid.startswith("uuid:"), f"实际 {sid}")
    c.check("订阅表有 1 条", len(s.subs) == 1)

    c.check("续订已知 SID 成功", s.renew(sid) is True)
    c.check("续订未知 SID 失败", s.renew("uuid:nope") is False)

    targets = s.take_targets()
    c.check("能取到 1 个推送目标", len(targets) == 1, f"实际 {len(targets)}")
    c.check("SEQ 从 1 开始", targets[0][2] == 1, f"实际 {targets[0][2]}")
    targets2 = s.take_targets()
    c.check("SEQ 自增到 2", targets2[0][2] == 2, f"实际 {targets2[0][2]}")

    c.check("退订成功", s.unsubscribe(sid) is True)
    c.check("退订后表为空", len(s.subs) == 0)
    c.check("重复退订返回 False", s.unsubscribe(sid) is False)

    # 事件体可被 XML 解析，且 LastChange 里能取回 TransportState
    body = last_change_body("STOPPED", "http://cdn/ep01.m3u8", "00:39:12", "NORMAL")
    ok_parse = True
    state_val = None
    try:
        root = ET.fromstring(body)
        prop = root.find("{urn:schemas-upnp-org:event-1-0}property")
        lc = prop.find("LastChange")
        inner = ET.fromstring(lc.text)
        for ev in inner.iter():
            if ev.tag.endswith("TransportState"):
                state_val = ev.get("val")
    except Exception as e:
        ok_parse = False
        print(f"    解析异常: {e}")
    c.check("事件体是合法 XML", ok_parse, "XML 解析失败")
    c.check("LastChange 里能取回 TransportState=STOPPED",
            state_val == "STOPPED", f"实际 {state_val}")

    # 特殊字符必须转义，否则客户端 XML 解析直接失败
    body2 = last_change_body("STOPPED", "http://cdn/a?x=1&y=2<b>", "00:00:10")
    ok2 = True
    try:
        ET.fromstring(body2)
    except Exception:
        ok2 = False
    c.check("URI 里的 & < > 被正确转义", ok2, "特殊字符破坏 XML")

    # ---- 分流行为：两个服务各自订阅，事件不能串 ----
    s2 = SubModel()
    sid_avt = s2.subscribe("http://192.168.1.5:1111/avt", service="AVTransport")
    sid_rcs = s2.subscribe("http://192.168.1.5:2222/rcs", service="RenderingControl")

    avt_only = s2.take_targets("AVTransport")
    c.check("按 AVTransport 只取到 1 个（不串入 RenderingControl）",
            len(avt_only) == 1, f"实际 {len(avt_only)}")
    c.check("取到的是 AVTransport 那个订阅",
            len(avt_only) == 1 and avt_only[0][0] == sid_avt,
            f"实际 {avt_only[0][0] if avt_only else None}")

    rcs_only = s2.take_targets("RenderingControl")
    c.check("按 RenderingControl 只取到 1 个",
            len(rcs_only) == 1, f"实际 {len(rcs_only)}")
    c.check("取到的是 RenderingControl 那个订阅",
            len(rcs_only) == 1 and rcs_only[0][0] == sid_rcs,
            f"实际 {rcs_only[0][0] if rcs_only else None}")

    # service=None 是调试用的广播，必须两个都给
    c.check("service=None 时广播给全部（2 个）",
            len(s2.take_targets()) == 2, "广播语义丢了")

    # eventSubURL → 服务名
    for path, want in [
        ("/upnp/event/AVTransport", "AVTransport"),
        ("/upnp/event/RenderingControl", "RenderingControl"),
        ("/upnp/event/AVTransport1", "AVTransport"),      # 带实例号
        ("/upnp/event/ConnectionManager", "ConnectionManager"),
        ("/upnp/event/unknown", "AVTransport"),           # 兜底
    ]:
        got = SubModel.service_name_from_event_url(path)
        c.check(f"{path} → {want}", got == want, f"实际 {got}")

    return 0 if c.summary() else 1


if __name__ == "__main__":
    sys.exit(main())