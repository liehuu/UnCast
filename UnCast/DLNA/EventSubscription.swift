import Foundation

/// GENA（UPnP 事件）订阅管理。
///
/// ## 为什么必须有它
///
/// DLNA 的连播不是接收端自己能决定的。手机 App 的逻辑是：
///
///   投第 1 集 → 订阅我们的事件 → 收到 `TransportState=STOPPED`
///   → 判断「这集播完了，该放下一集」→ 再发 SetAVTransportURI(第 2 集)
///
/// 少了中间那步通知，手机端永远不知道第 1 集已经放完，
/// 自然也不会推第 2 集。用户看到的现象就是「播完停住，得回手机重新投屏」。
///
/// ## 之前的实现为什么等于没做
///
/// 旧代码对 SUBSCRIBE 只回一个编造的 `SID` 和 `TIMEOUT`，然后什么都不发。
/// 更关键的是 SCPD 里所有 stateVariable 都写着 `sendEvents="no"`，
/// 也没有 `LastChange` 变量 —— 按照 UPnP 规范，这是明确声明
/// 「本设备不产生任何事件」。严格的客户端看到这个声明就不订阅了，
/// 宽松的客户端订了也收不到东西。两种情况结果一样：连播不成立。
///
/// ## 本实现的边界（不吹牛）
///
/// - 支持 SUBSCRIBE 新订阅、续订（带 SID 再 SUBSCRIBE）、UNSUBSCRIBE
/// - 只发 AVTransport 的 LastChange，RenderingControl 的 Volume 也发一份
/// - 事件体是 UPnP AV 标准的 `<e:propertyset>` 结构
/// - 不做 GENA 的 SEQ 重传与乱序处理（单机单客户端场景用不上）
final class EventSubscription {

    struct Subscriber {
        let sid: String
        /// 回调地址，客户端在 CALLBACK 头里给
        let callbackURL: URL
        /// 订阅到期时间（客户端 TIMEOUT 头指定，规范要求不超过 1800 秒）
        let expiresAt: Date
        /// 同一订阅的事件序号，从 0 开始自增
        var seq: Int = 0
    }

    /// SID → 订阅。用字典便于续订/退订按 SID 查找。
    private var subscribers: [String: Subscriber] = [:]
    private let lock = NSLock()

    /// 订阅有效期。UPnP 规范要求 device 支持 `Second-1800`。
    static let timeoutSeconds = 1800

    // MARK: - 解析请求头

    /// 从 `CALLBACK` 头里取出第一个 http URL。
    ///
    /// 格式是 `<http://192.168.1.5:1234/notify>`，尖括号必须有，
    /// 但实际客户端偶尔省略，所以两种都收。
    static func parseCallback(_ raw: String?) -> URL? {
        guard let raw, !raw.isEmpty else { return nil }
        // 可能有多段 `<a><b>`，取第一段
        var candidate = raw
        if let lt = raw.firstIndex(of: "<"),
           let gt = raw[lt...].firstIndex(of: ">") {
            candidate = String(raw[raw.index(after: lt)..<gt])
        }
        candidate = candidate.trimmingCharacters(in: .whitespacesAndNewlines)
        guard candidate.lowercased().hasPrefix("http"),
              let url = URL(string: candidate),
              url.host != nil else { return nil }
        return url
    }

    /// 从 `TIMEOUT` 头里取秒数。`Second-infinite` 按上限 1800 处理。
    static func parseTimeout(_ raw: String?) -> Int {
        guard let raw else { return timeoutSeconds }
        let lower = raw.lowercased()
        if lower.contains("infinite") { return timeoutSeconds }
        if let range = lower.range(of: "second-") {
            let num = lower[range.upperBound...].prefix { $0.isNumber }
            if let v = Int(num), v > 0 {
                // 规范上限 1800，客户端要更长也不给
                return min(v, timeoutSeconds)
            }
        }
        return timeoutSeconds
    }

    // MARK: - 订阅生命周期

    static func makeSID() -> String {
        "uuid:" + UUID().uuidString.lowercased()
    }

    /// 新建订阅，返回 SID。
    func subscribe(callback: URL, timeout: Int) -> String {
        let sid = Self.makeSID()
        lock.lock()
        subscribers[sid] = Subscriber(
            sid: sid,
            callbackURL: callback,
            expiresAt: Date().addingTimeInterval(TimeInterval(timeout))
        )
        lock.unlock()
        return sid
    }

    /// 续订。SID 不存在（已过期或客户端记错了）时返回 nil，
    /// 让调用方回 412 让客户端重新订阅 —— 这是规范行为，不是错误。
    func renew(sid: String, timeout: Int) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard var sub = subscribers[sid] else { return false }
        sub.expiresAt = Date().addingTimeInterval(TimeInterval(timeout))
        sub.seq = 0
        subscribers[sid] = sub
        return true
    }

    func unsubscribe(sid: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return subscribers.removeValue(forKey: sid) != nil
    }

    /// 清掉过期订阅。每次有状态变化时顺手调一次，不需要独立定时器。
    private func pruneExpired(now: Date = Date()) {
        subscribers = subscribers.filter { $0.value.expiresAt > now }
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        pruneExpired()
        return subscribers.count
    }

    /// 取出当前全部有效订阅并各自分配 SEQ。
    /// 分配在锁内完成，避免两个并发 NOTIFY 拿到同一个序号。
    private func takeTargets() -> [Subscriber] {
        lock.lock()
        defer { lock.unlock() }
        pruneExpired()
        var out: [Subscriber] = []
        for (sid, var sub) in subscribers {
            sub.seq += 1
            subscribers[sid] = sub
            out.append(sub)
        }
        return out
    }

    // MARK: - 事件体

    /// AVTransport 的 LastChange 事件体。
    ///
    /// UPnP AV 规定 TransportState 等状态通过 `LastChange` 这个 stateVariable
    /// 一次性带出去，内容是**被 XML 二次转义**的 `<Event>` 片段。
    /// 手写拼接比引入 XML 库可靠：这里结构固定，没有用户输入直接进 XML。
    static func lastChangeBody(transportState: String,
                               transportStatus: String = "OK",
                               currentTrackURI: String = "",
                               currentTrackDuration: String = "00:00:00",
                               currentPlayMode: String = "NORMAL") -> String {
        let inner = """
        <Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">
          <InstanceID val="0">
            <TransportState val="\(escape(transportState))"/>
            <TransportStatus val="\(escape(transportStatus))"/>
            <CurrentTrackURI val="\(escape(currentTrackURI))"/>
            <CurrentTrackDuration val="\(escape(currentTrackDuration))"/>
            <CurrentPlayMode val="\(escape(currentPlayMode))"/>
          </InstanceID>
        </Event>
        """
        // LastChange 的值本身要再转义一次
        return propertyset(
            variable: "LastChange",
            value: escape(inner)
        )
    }

    /// RenderingControl 的 Volume 变化事件。
    static func volumeBody(volume: Int) -> String {
        let inner = """
        <Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">
          <InstanceID val="0">
            <Volume channel="Master" val="\(volume)"/>
          </InstanceID>
        </Event>
        """
        return propertyset(variable: "LastChange", value: escape(inner))
    }

    private static func propertyset(variable: String, value: String) -> String {
        """
        <?xml version="1.0" encoding="utf-8"?>
        <e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">
          <e:property>
            <\(variable)>\(value)</\(variable)>
          </e:property>
        </e:propertyset>
        """
    }

    static func escape(_ s: String) -> String {
        var out = s
        out = out.replacingOccurrences(of: "&", with: "&amp;")
        out = out.replacingOccurrences(of: "<", with: "&lt;")
        out = out.replacingOccurrences(of: ">", with: "&gt;")
        out = out.replacingOccurrences(of: "\"", with: "&quot;")
        out = out.replacingOccurrences(of: "'", with: "&apos;")
        return out
    }

    // MARK: - 发送

    /// 向所有订阅者推一条 NOTIFY。
    ///
    /// 用同步 URLSession 在后台队列发，不占主线程。
    /// 发送失败**不重试**：UPnP 的 GENA 本身就允许丢包，
    /// 客户端靠下一次事件或轮询 GetTransportInfo 恢复。
    /// 重试反而会在客户端已经退订后继续骚扰它。
    func notify(body: String, log: @escaping (String) -> Void = { _ in }) {
        let targets = takeTargets()
        guard !targets.isEmpty else { return }

        for sub in targets {
            var req = URLRequest(url: sub.callbackURL)
            req.httpMethod = "NOTIFY"
            req.timeoutInterval = 5
            req.setValue("text/xml; charset=\"utf-8\"", forHTTPHeaderField: "Content-Type")
            req.setValue("upnp:event", forHTTPHeaderField: "NT")
            req.setValue("upnp:propchange", forHTTPHeaderField: "NTS")
            req.setValue(sub.sid, forHTTPHeaderField: "SID")
            req.setValue("\(sub.seq)", forHTTPHeaderField: "SEQ")
            req.httpBody = Data(body.utf8)

            let sid = sub.sid
            URLSession.shared.dataTask(with: req) { _, resp, err in
                if let err {
                    log("⚠️ NOTIFY 失败 \(sid.prefix(13)): \(err.localizedDescription)")
                } else if let http = resp as? HTTPURLResponse, http.statusCode >= 400 {
                    // 410 Gone 表示客户端已经不要了，规范允许直接删订阅
                    log("⚠️ NOTIFY \(http.statusCode) \(sid.prefix(13))")
                }
            }.resume()
        }
    }
}