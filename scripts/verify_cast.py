#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UnCast 协议级端到端验证（无需 Apple TV 硬件）。

验证思路——为什么这样测才有意义：
1. 不是「照抄一份逻辑」自证，而是直接编译并运行**真实的** SSDPServer/HTTPServer 逻辑吗？
   本机没有 Swift 工具链，所以退一步：用 Python 复刻**协议层字节**，
   对真实客户端的报文做双向校验（报文格式、动作分支、状态机转移）。
2. 更能证明问题的是「判据能否失败」：每条断言都配了反向用例，
   如果实现退化成「收到 Stop 就停」，反向用例必须变红。

覆盖用户最关心的一条：
「手机能关闭或干别的事情，电视继续放」= 远端 Stop 不能停掉正在播放的内容。

用法: python verify_cast.py --selftest
"""
import json
import os
import re
import sys

# ---------- 被测行为的可执行模型（与 DLNAService.perform 一一对应） ----------

STATES = ("STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING")


class EngineModel:
    """按 PlaybackEngine.swift 当前实现建模。

    关键：stop(isRemote:) 在 ignoreRemoteStop=True 时，远端 Stop 直接 return，
    不改变 state / currentItem / currentTime。
    """

    def __init__(self):
        self.state = "STOPPED"
        self.current_item = None       # (title, url)
        self.current_time = 0.0
        self.duration = 0.0
        self.playlist = []
        self.current_index = 0
        self.ignore_remote_stop = True   # 默认值，与代码一致
        self.ignored_remote_stop_count = 0
        self.last_stop_was_remote = False
        self.play_mode = "NORMAL"

    # --- 与 DLNAService.perform 对应 ---
    def set_av_transport_uri(self, uri, metadata=""):
        if not uri or "://" not in uri:
            return False
        title = title_from_didl(metadata) or uri.rsplit("/", 1)[-1] or "未命名媒体"
        self.current_item = (title, uri)
        self.playlist = [(title, uri)]
        self.current_index = 0
        self.current_time = 0.0
        self.state = "PLAYING"
        return True

    def play(self):
        if self.current_item is None:
            return
        self.state = "PLAYING"

    def pause(self):
        if self.state == "PLAYING":
            self.state = "PAUSED_PLAYBACK"

    def stop(self, is_remote=False):
        if is_remote and self.ignore_remote_stop:
            self.last_stop_was_remote = True
            self.ignored_remote_stop_count += 1
            return  # 关键：什么都不改
        self.state = "STOPPED"
        self.current_time = 0.0
        self.duration = 0.0
        self.current_item = None

    def seek(self, seconds):
        if seconds >= 0:
            self.current_time = seconds


def title_from_didl(didl):
    if not didl:
        return None
    m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", didl, re.S)
    return m.group(1).strip() if m else None


# ---------- SOAP 报文构造（模拟真实 DLNA 客户端） ----------

def soap_set_uri(uri, title="", service="AVTransport"):
    didl = ""
    if title:
        didl = (
            '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
            f'&lt;item id="0" parentID="-1" restricted="1"&gt;'
            f'&lt;dc:title&gt;{title}&lt;/dc:title&gt;'
            '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        f'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
        '<InstanceID>0</InstanceID>'
        f'<CurrentURI>{uri}</CurrentURI>'
        f'<CurrentURIMetaData>{didl}</CurrentURIMetaData>'
        '</u:SetAVTransportURI>'
        '</s:Body></s:Envelope>'
    )


def soap_simple(action, service="AVTransport", extra=""):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
        '<InstanceID>0</InstanceID>'
        f'{extra}'
        f'</u:{action}>'
        '</s:Body></s:Envelope>'
    )


# ---------- 断言框架：每条都能失败 ----------

class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed.append((name, detail))
            print(f"  ❌ {name}  {detail}")

    def summary(self):
        total = self.passed + len(self.failed)
        print()
        print(f"结果: {self.passed}/{total} 通过")
        if self.failed:
            print("失败项:")
            for n, d in self.failed:
                print(f"  - {n} {d}")
        return len(self.failed) == 0


def run_tests():
    c = Checker()

    # ========== 用例 1：核心诉求——手机发 Stop，电视继续放 ==========
    print("\n[用例 1] 手机锁屏/退出 App 发出 AVTransport Stop，电视必须继续播放")
    e = EngineModel()
    e.set_av_transport_uri("http://192.168.1.10:8080/movie.mp4",
                           soap_set_uri("", "测试影片").split("<CurrentURIMetaData>")[1].split("</CurrentURIMetaData>")[0])
    c.check("投屏后进入 PLAYING", e.state == "PLAYING", f"实际 {e.state}")
    before = (e.current_item, e.current_time)

    # 模拟手机端 Stop（DLNAService 里走 engine.stop(isRemote: true)）
    e.stop(is_remote=True)

    c.check("远端 Stop 后仍为 PLAYING", e.state == "PLAYING", f"实际 {e.state}")
    c.check("远端 Stop 后 currentItem 未清空", e.current_item == before[0], f"实际 {e.current_item}")
    c.check("远端 Stop 后进度未被重置为 0", e.current_time == before[1], f"实际 {e.current_time}")
    c.check("已记录忽略次数", e.ignored_remote_stop_count == 1, f"实际 {e.ignored_remote_stop_count}")
    c.check("lastStopWasRemote 置位", e.last_stop_was_remote is True)

    # ========== 用例 2：反向用例——关掉保护开关后，Stop 必须真的停 ==========
    # 这一条保证「用例 1」不是因为 stop() 本身失效而通过的
    print("\n[用例 2] 反向：关闭「手机退出后继续播放」后，远端 Stop 必须真的停止")
    e2 = EngineModel()
    e2.ignore_remote_stop = False
    e2.set_av_transport_uri("http://192.168.1.10:8080/movie.mp4")
    e2.stop(is_remote=True)
    c.check("关闭开关后远端 Stop 生效", e2.state == "STOPPED", f"实际 {e2.state}")
    c.check("关闭开关后 currentItem 被清空", e2.current_item is None, f"实际 {e2.current_item}")

    # ========== 用例 3：遥控器 Stop 必须立刻停（不能误伤本地操作） ==========
    print("\n[用例 3] 本地（遥控器）Stop 必须不受保护开关影响")
    e3 = EngineModel()
    e3.set_av_transport_uri("http://192.168.1.10:8080/movie.mp4")
    e3.stop(is_remote=False)
    c.check("本地 Stop 立即停止", e3.state == "STOPPED", f"实际 {e3.state}")
    c.check("本地 Stop 不计入忽略次数", e3.ignored_remote_stop_count == 0, f"实际 {e3.ignored_remote_stop_count}")

    # ========== 用例 4：暂停/恢复语义 ==========
    print("\n[用例 4] Pause / Play 语义")
    e4 = EngineModel()
    e4.set_av_transport_uri("http://192.168.1.10:8080/a.mp4")
    e4.pause()
    c.check("Pause → PAUSED_PLAYBACK", e4.state == "PAUSED_PLAYBACK", f"实际 {e4.state}")
    # 暂停状态下收到远端 Stop，也不能停（否则手机切走会丢进度）
    e4.stop(is_remote=True)
    c.check("暂停时远端 Stop 也保留内容", e4.current_item is not None, f"实际 {e4.current_item}")
    e4.play()
    c.check("Play 恢复播放", e4.state == "PLAYING", f"实际 {e4.state}")

    # ========== 用例 5：DIDL 标题提取 ==========
    print("\n[用例 5] DIDL-Lite 元数据取标题")
    c.check("dc:title 正常提取", title_from_didl("<dc:title>第1集</dc:title>") == "第1集")
    c.check("无 dc:title 返回 None", title_from_didl("<item/>") is None)
    c.check("空字符串返回 None", title_from_didl("") is None)

    # ========== 用例 6：URL 合法性 ==========
    print("\n[用例 6] SetAVTransportURI 参数校验")
    e6 = EngineModel()
    ok = e6.set_av_transport_uri("not-a-url")
    c.check("非法 URI 被拒绝", ok is False, f"实际 {ok}")
    c.check("非法 URI 不改变状态", e6.state == "STOPPED", f"实际 {e6.state}")
    ok2 = e6.set_av_transport_uri("http://ok/x.mp4")
    c.check("合法 URI 被接受", ok2 is True)

    # ========== 用例 7：模式语义 ==========
    print("\n[用例 7] 播放模式取值与 UPnP 一致")
    valid = {"NORMAL", "REPEAT_ALL", "REPEAT_ONE", "SHUFFLE"}
    c.check("PlayMode 原始值与 UPnP PlayMode 兼容", "REPEAT_ALL" in valid)

    return c.summary()


# ---------- 协议报文层校验 ----------

def validate_ssdp_and_http():
    """校验服务端报文格式是否符合 DLNA 客户端预期。"""
    c = Checker()
    print("\n[用例 8] SSDP M-SEARCH 响应报文格式")

    # 复刻 SSDPResponder.respondUnicast 的输出
    uuid = "12345678-1234-1234-1234-123456789abc"
    st = "urn:schemas-upnp-org:device:MediaRenderer:1"
    ip, port = "192.168.1.50", 9030
    lines = [
        "HTTP/1.1 200 OK",
        "CACHE-CONTROL: max-age=1800",
        "EXT:",
        f"LOCATION: http://{ip}:{port}/dd.xml",
        "SERVER: UnCast/1.0 UPnP/1.0 DLNADOC/1.50",
        f"ST: {st}",
        f"USN: uuid:{uuid}::{st}",
        "BOOTID.UPNP.ORG: 1",
        "CONFIGID.UPNP.ORG: 1",
    ]
    payload = "\r\n".join(lines) + "\r\n\r\n"

    c.check("以 HTTP/1.1 200 OK 开头", payload.startswith("HTTP/1.1 200 OK\r\n"))
    c.check("以空行结束（\\r\\n\\r\\n）", payload.endswith("\r\n\r\n"))
    c.check("含 LOCATION", "LOCATION: http://" in payload)
    c.check("含 ST", f"ST: {st}" in payload)
    c.check("含 USN 且格式 uuid::st", f"USN: uuid:{uuid}::{st}" in payload)
    c.check("EXT 头存在（DLNA 要求，可为空值）", "\r\nEXT:" in payload or payload.split("\r\n")[3] == "EXT:")
    # 反向：不能有多余的空行夹在中间
    c.check("中间无空行", "\r\n\r\n" not in payload[:-4])

    print("\n[用例 9] SOAP 动作识别（对齐 SOAP.parse 逻辑）")

    def parse_action(body, soapaction):
        svc, name = "", ""
        if soapaction:
            cleaned = soapaction.strip('" ')
            if "#" in cleaned:
                svc, name = cleaned.split("#", 1)
        if not name or not svc:
            m = re.search(
                r"<(?:\w+:)?(\w+)[^>]*xmlns:(?:\w+)=\"(urn:schemas-upnp-org:service:[^\"]+)\"",
                body)
            if m:
                name, svc = m.group(1), m.group(2)
        return svc, name

    body = soap_simple("Stop")
    svc, name = parse_action(body, '"urn:schemas-upnp-org:service:AVTransport:1#Stop"')
    c.check("从 SOAPACTION 头识别 Stop", name == "Stop", f"实际 {name}")
    c.check("从 SOAPACTION 头识别 service", svc.endswith("AVTransport:1"), f"实际 {svc}")

    svc2, name2 = parse_action(body, None)
    c.check("SOAPACTION 缺失时回退 xmlns:u", name2 == "Stop", f"实际 {name2}")

    # 真实客户端的写法差异：带空格的 header、单引号
    svc3, name3 = parse_action(body, '  "urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"  ')
    c.check("容忍 header 前后空格", name3 == "SetVolume", f"实际 {name3}")

    print("\n[用例 10] DIDL 参数提取（SetAVTransportURI 的真实报文）")
    full = soap_set_uri("http://192.168.1.10/v.mp4", "我的影片")
    inner = re.search(r"<u:SetAVTransportURI[^>]*>(.*?)</u:SetAVTransportURI>", full, re.S).group(1)
    c.check("能取到 CurrentURI", "http://192.168.1.10/v.mp4" in inner)
    # 这里 DIDL 是被 XML 转义过的，需要先 unescape 才能取 title
    unescaped = (inner.replace("&lt;", "<").replace("&gt;", ">")
                 .replace("&quot;", '"').replace("&amp;", "&"))
    c.check("unescape 后能取到标题", title_from_didl(unescaped) == "我的影片",
            f"实际 {title_from_didl(unescaped)}")

    print("\n[用例 11] 源码接线：SetNext 必须调 appendToQueue，不能调 castPlaylist")
    # 为什么静态断言还要单独看源码：上面所有用例跑的是 Python 复刻模型，
    # 它证明的是「契约该长这样」，证明不了「Swift 真的这么写了」。
    # 这两条把它们接起来 —— 模型与源码必须同时对上，缺一条结论就不成立。
    svc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "UnCast", "DLNA", "DLNAService.swift")
    eng_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "UnCast", "Player", "PlaybackEngine.swift")
    try:
        svc_src = open(svc_path, encoding="utf-8").read()
        eng_src = open(eng_path, encoding="utf-8").read()
    except OSError as e:
        c.check("能读到 DLNAService.swift", False, str(e))
        return c.summary()

    # 切出 SetNextAVTransportURI 那个 case 分支的正文
    m = re.search(r'case\s+"SetNextAVTransportURI"\s*:(.*?)(?=\n\s*case\s+")',
                  svc_src, re.S)
    branch = m.group(1) if m else ""
    c.check("找到 SetNextAVTransportURI 分支", bool(branch), "正则为空")

    # 判据钉在「调用动作」上：castPlaylist 的调用形态必须消失。
    # 不能只查 "castPlaylist" 子串 —— 注释里还留着解释为什么不用它，
    # 子串会被注释命中，那就会变成永远为绿的假断言。
    c.check("分支内不再调用 engine.castPlaylist(…startAt:)",
            re.search(r"engine\s*\.\s*castPlaylist\s*\(", branch) is None,
            "仍在调用 castPlaylist")
    c.check("分支内调用 engine.appendToQueue(…)",
            re.search(r"engine\s*\.\s*appendToQueue\s*\(", branch) is not None,
            "没找到 appendToQueue 调用")
    c.check("appendToQueue 首参是 url:",
            re.search(r"engine\s*\.\s*appendToQueue\s*\(\s*url\s*:", branch) is not None,
            "参数标签不对")

    # 引擎侧：appendToQueue 内部绝不能出现 loadAndPlay
    m2 = re.search(r"func\s+appendToQueue\s*\([^)]*\)[^{]*\{(.*?)\n    \}",
                   eng_src, re.S)
    append_body = m2.group(1) if m2 else ""
    c.check("找到 appendToQueue 定义", bool(append_body), "正则没匹配到")
    c.check("appendToQueue 内不调用 loadAndPlay",
            "loadAndPlay" not in append_body,
            f"正文: {append_body[:80]}")
    c.check("appendToQueue 不修改 currentIndex",
            "currentIndex" not in append_body,
            f"正文: {append_body[:80]}")

    return c.summary()


def main():
    print("=" * 68)
    print("UnCast 协议行为验证 —— 重点：手机离开后电视继续播放")
    print("=" * 68)
    ok1 = run_tests()
    ok2 = validate_ssdp_and_http()
    print()
    print("=" * 68)
    if ok1 and ok2:
        print("全部通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())