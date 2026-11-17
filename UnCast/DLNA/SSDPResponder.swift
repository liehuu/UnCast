import Foundation
import Darwin

/// SSDP (Simple Service Discovery Protocol) 服务端。
///
/// 职责：
/// 1. 加入组播组 239.255.255.250:1900，监听 M-SEARCH 请求
/// 2. 对 `ssdp:all` / `urn:schemas-upnp-org:device:MediaRenderer:1` 等搜索回复 200 OK
/// 3. 周期性发送 NOTIFY alive / byebye 通告
///
/// 关键实现细节（踩坑记录）：
/// - 必须设置 SO_REUSEADDR + SO_REUSEPORT，否则与其它 DLNA 服务抢不到 1900 端口
/// - 组播加入用 IP_ADD_MEMBERSHIP，接口必须指定为 en0（Apple TV 的以太网/WiFi）
/// - 回复必须用单播（unicast）发回请求方地址，不能用组播回复，否则手机收不到
/// - CFSocket 在 tvOS 上可用，但直接用 BSD socket 更可控
final class SSDPResponder {

    // MARK: - 配置

    struct Config {
        var multicastGroup: String = "239.255.255.250"
        var port: UInt16 = 1900
        /// 本机对外提供 device description 的 HTTP 端口
        var httpPort: UInt16 = 9030
        /// 设备 UUID，应持久化以保证设备识别稳定
        var uuid: String
        var friendlyName: String

        /// 显式 init：memberwise init 要求实参严格按声明顺序，
        /// 调用方按语义顺序（uuid → friendlyName → httpPort）传更自然，
        /// 这里给出一个顺序无关的入口。
        init(uuid: String,
             friendlyName: String,
             httpPort: UInt16 = 9030,
             multicastGroup: String = "239.255.255.250",
             port: UInt16 = 1900) {
            self.uuid = uuid
            self.friendlyName = friendlyName
            self.httpPort = httpPort
            self.multicastGroup = multicastGroup
            self.port = port
        }
    }

    // MARK: - 通告目录（SSDP 的单一事实源）

    /// 本机对外通告的全部 search target（NT）。
    ///
    /// 这是唯一事实源：`replyTargets` 用它回答 `ssdp:all`，
    /// `sendNotify` 用它逐条发 NOTIFY。改这里等于同时改发现与通告。
    ///
    /// 为什么必须逐个通告，而不是只认一个：
    /// 客户端常以 `ST: ssdp:all` 搜索，然后**只接受 ST 恰好等于
    /// `urn:schemas-upnp-org:device:MediaRenderer:1` 的那条回复**。
    /// 早先版本收到 `ssdp:all` 时原样回一条 `ST: ssdp:all`，
    /// 严格实现的客户端会判定「这不是我要的设备类型」并丢弃整条回复 ——
    /// 于是设备在它的列表里就是不出现，而 M-SEARCH 日志里却明明有记录。
    static let searchTargets: [String] = [
        "upnp:rootdevice",
        "urn:schemas-upnp-org:device:MediaRenderer:1",
        "urn:schemas-upnp-org:service:AVTransport:1",
        "urn:schemas-upnp-org:service:RenderingControl:1",
        "urn:schemas-upnp-org:service:ConnectionManager:1"
    ]

    /// 通告有效期。NOTIFY 的周期必须是它的 1/2，否则两次通告之间缓存就过期了：
    /// 手机过一会儿再翻设备列表，设备已经不在缓存里。
    static let maxAge = 1800
    static let notifyInterval: TimeInterval = Double(maxAge) / 2

    /// 每次进程启动递增。
    ///
    /// UPnP 1.1 规定 BOOTID 变化时客户端必须丢弃旧缓存重新拉描述。
    /// 写死常量的话，App 重启后换端口/换服务列表，客户端还在用旧缓存连不上。
    private static let bootID: Int = {
        let key = "uncast.ssdp.bootid"
        let next = UserDefaults.standard.integer(forKey: key) + 1
        UserDefaults.standard.set(next, forKey: key)
        return next
    }()

    private let config: Config
    private var socketFD: Int32 = -1
    private var recvThread: Thread?
    private var notifyThread: Thread?
    private var isRunning = false
    private let log: (String) -> Void

    /// 统计回调。参数是事件类型，由上层累加到 @Published 计数。
    ///
    /// 用回调而不是让本类持有计数器：本类跑在后台线程，
    /// 直接改 @Published 会触发 SwiftUI 的「不在主线程发布」问题。
    /// 上层的 appendLog 已经统一派发到主线程，走同一条路最省事。
    enum Event {
        case search          // 收到一条 M-SEARCH
        case reply(Int)      // 回了几条 200 OK
        case notify(Int)     // 发了几条 NOTIFY
    }
    private let onEvent: (Event) -> Void

    init(config: Config,
         log: @escaping (String) -> Void = { _ in },
         onEvent: @escaping (Event) -> Void = { _ in }) {
        self.config = config
        self.log = log
        self.onEvent = onEvent
    }

    // MARK: - 生命周期

    func start() throws {
        guard !isRunning else { return }

        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { throw SSDPError.socketCreate(errno) }

        var yes: Int32 = 1
        // 必须复用地址，否则 1900 已被占用时 bind 失败
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &yes, socklen_t(MemoryLayout<Int32>.size))

        // 允许接收组播
        var ttl: UInt8 = 4
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, socklen_t(MemoryLayout<UInt8>.size))

        // 绑定 0.0.0.0:1900
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = config.port.bigEndian
        addr.sin_addr.s_addr = INADDR_ANY

        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else {
            close(fd)
            throw SSDPError.bindFailed(errno)
        }

        // 加入组播组（在所有活跃 IPv4 接口上尝试，Apple TV 可能用 en0 或 en1）
        try joinMulticast(fd: fd, group: config.multicastGroup)

        self.socketFD = fd
        isRunning = true

        let t = Thread { [weak self] in self?.receiveLoop() }
        t.name = "ssdp.recv"
        t.qualityOfService = .userInitiated
        t.start()
        recvThread = t

        // 周期通告。必须在 isRunning 置真之后启动，
        // 否则循环体第一次判断就退出了。
        startNotifyLoop()

        log("SSDP 监听中 \(config.multicastGroup):\(config.port)，\(Self.searchTargets.count) 个 target")
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        sendByeBye()
        if socketFD >= 0 {
            close(socketFD)
            socketFD = -1
        }
        recvThread?.cancel()
        recvThread = nil
        notifyThread?.cancel()
        notifyThread = nil
        log("SSDP 已停止")
    }

    /// 发送 NOTIFY ssdp:alive 通告，让局域网设备立刻看到本设备
    func announceAlive() {
        sendNotify(nts: "ssdp:alive")
    }

    private func sendByeBye() {
        sendNotify(nts: "ssdp:byebye")
    }

    // MARK: - 组播

    private func joinMulticast(fd: Int32, group: String) throws {
        // 枚举所有 IPv4 接口，逐个尝试加入组播组
        var ifaddrPtr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddrPtr) == 0, let first = ifaddrPtr else {
            throw SSDPError.interfaceEnum
        }
        defer { freeifaddrs(ifaddrPtr) }

        var joined = 0
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = cursor {
            defer { cursor = cur.pointee.ifa_next }
            let ifa = cur.pointee
            guard let saPtr = ifa.ifa_addr, saPtr.pointee.sa_family == UInt8(AF_INET) else { continue }
            // 跳过回环与未启用接口
            let flags = Int32(ifa.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0 else { continue }

            // 显式拷贝出 sockaddr_in 再取 sin_addr，避免直接对原始指针做反弹
            let sinAddr: in_addr = withUnsafePointer(to: saPtr.pointee) { raw -> in_addr in
                raw.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
            }

            var mreq = ip_mreq()
            mreq.imr_multiaddr.s_addr = inet_addr(group)
            mreq.imr_interface = sinAddr

            let r = setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq,
                               socklen_t(MemoryLayout<ip_mreq>.size))
            if r == 0 {
                joined += 1
                let name = String(cString: ifa.ifa_name)
                log("加入组播组成功: \(name) (\(Self.ipString(sinAddr)))")
            }
        }

        guard joined > 0 else { throw SSDPError.multicastJoin }
    }

    // MARK: - 收包循环

    private func receiveLoop() {
        // 用 raw buffer 配 recvfrom，避免 Swift 数组指针桥接的类型问题
        let bufferSize = 2048
        let rawBuf = UnsafeMutableRawPointer.allocate(byteCount: bufferSize, alignment: 1)
        defer { rawBuf.deallocate() }

        while isRunning && socketFD >= 0 {
            var sender = sockaddr_in()
            var senderLen = socklen_t(MemoryLayout<sockaddr_in>.size)

            let n = withUnsafeMutablePointer(to: &sender) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sp in
                    recvfrom(socketFD, rawBuf, bufferSize, 0, sp, &senderLen)
                }
            }

            guard n > 0 else {
                // EINTR/EAGAIN 属正常，继续；其它错误短暂退避避免空转烧 CPU
                if errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK {
                    Thread.sleep(forTimeInterval: 0.05)
                }
                continue
            }

            let data = Data(bytes: rawBuf, count: n)
            guard let text = String(data: data, encoding: .utf8) else { continue }
            guard text.hasPrefix("M-SEARCH") else { continue }

            let st = Self.extractHeader(text, key: "ST") ?? ""
            let man = Self.extractHeader(text, key: "MAN") ?? ""
            // 只处理 ssdp:discover
            guard man.uppercased().contains("SSDP:DISCOVER") else { continue }

            let targets = replyTargets(for: st)
            guard !targets.isEmpty else { continue }

            let target = Self.ipString(sender.sin_addr)
            let targetPort = UInt16(bigEndian: sender.sin_port)
            onEvent(.search)
            onEvent(.reply(targets.count))
            log("M-SEARCH from \(target):\(targetPort) ST=\(st.prefix(48)) → \(targets.count) 条回复")

            for t in targets {
                respondUnicast(to: target, port: targetPort, st: t)
            }
        }
    }

    /// 根据客户端发来的 ST 决定要回哪些 ST。
    ///
    /// 关键点：`ssdp:all` / `upnp:rootdevice` **不能原样回显**。
    /// 客户端按 ST 匹配设备类型，回一条 `ST: ssdp:all` 等于告诉它
    /// 「这个设备是 ssdp:all 类型」——没有一个客户端认这个类型。
    /// 正确做法是对 `ssdp:all` 把每个具体 target 各回一条。
    private func replyTargets(for st: String) -> [String] {
        let s = st.lowercased()
        if s == "ssdp:all" {
            // UPnP 1.1 §1.2.3：ssdp:all 要为**每一类通告**各回一条，
            // 其中包括裸 UDN（ST/USN 都是 uuid:xxx）。
            // 少数基于 Cling 的老客户端正是靠这条认设备的，不能省。
            return ["uuid:\(config.uuid)"] + Self.searchTargets
        }
        if s == "upnp:rootdevice" { return ["upnp:rootdevice"] }
        if s == "uuid:\(config.uuid.lowercased())" { return ["uuid:\(config.uuid)"] }
        // 精确命中某个 target 时回它自己
        for known in Self.searchTargets where s == known.lowercased() {
            return [known]
        }
        // 版本号不同的（如 MediaRenderer:2）也接受，但回客户端请求的那个版本号，
        // 否则它同样不认。
        if s.hasPrefix("urn:schemas-upnp-org:device:mediarenderer")
            || s.hasPrefix("urn:schemas-upnp-org:service:avtransport")
            || s.hasPrefix("urn:schemas-upnp-org:service:renderingcontrol")
            || s.hasPrefix("urn:schemas-upnp-org:service:connectionmanager") {
            return [st]
        }
        return []
    }

    /// 单播回复 M-SEARCH。必须发到请求来源的 IP:端口，不能往 1900 组播回。
    private func respondUnicast(to ip: String, port: UInt16, st: String) {
        let location = "http://\(Self.localIPAddress()):\(config.httpPort)/dd.xml"
        let target = st.isEmpty ? "urn:schemas-upnp-org:device:MediaRenderer:1" : st

        let lines = [
            "HTTP/1.1 200 OK",
            "CACHE-CONTROL: max-age=\(Self.maxAge)",
            "DATE: \(Self.httpDate())",
            "EXT:",
            "LOCATION: \(location)",
            "SERVER: \(Self.serverString)",
            "ST: \(target)",
            "USN: \(usn(for: target))",
            "BOOTID.UPNP.ORG: \(Self.bootID)",
            "CONFIGID.UPNP.ORG: 1"
        ]
        // SSDP 报文必须以空行（\r\n\r\n）结尾
        let payload = lines.joined(separator: "\r\n") + "\r\n\r\n"
        sendUDP(payload, to: ip, port: port)
    }

    /// USN 规则（UPnP 1.1 §1.2.2）：
    /// - 裸 UDN 那一条：`NT = uuid:<udn>`、`USN = uuid:<udn>`（**不加后缀**）
    /// - 其余 target：`USN = uuid:<udn>::<target>`
    ///
    /// 裸 UDN 也加 ::uuid:xxx 后缀的话，同一条通告会被客户端解析成两个不同的 USN，
    /// 设备列表里就会出现一个认不出类型的幽灵条目。
    private func usn(for target: String) -> String {
        if target.lowercased().hasPrefix("uuid:") { return target }
        return "uuid:\(config.uuid)::\(target)"
    }

    /// RFC 1123 日期。UPnP 1.1 起 DATE 头是必须的，
    /// 缺了它部分客户端会认为响应不可信（无法判断缓存新鲜度）。
    static func httpDate() -> String {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = TimeZone(identifier: "GMT")
        fmt.dateFormat = "EEE, dd MMM yyyy HH:mm:ss 'GMT'"
        return fmt.string(from: Date())
    }

    static let serverString = "Darwin/1.0 UPnP/1.1 UnCast/1.0"

    private func sendNotify(nts: String) {
        let location = "http://\(Self.localIPAddress()):\(config.httpPort)/dd.xml"
        // 裸 UDN 那条也必须通告，否则客户端不会把「设备本身」记进缓存。
        let targets = ["uuid:\(config.uuid)"] + Self.searchTargets
        for nt in targets {
            let lines = [
                "NOTIFY * HTTP/1.1",
                "HOST: \(config.multicastGroup):\(config.port)",
                "CACHE-CONTROL: max-age=\(Self.maxAge)",
                "LOCATION: \(location)",
                "NT: \(nt)",
                "NTS: \(nts)",
                "SERVER: \(Self.serverString)",
                "USN: \(usn(for: nt))",
                "BOOTID.UPNP.ORG: \(Self.bootID)",
                "CONFIGID.UPNP.ORG: 1"
            ]
            let payload = lines.joined(separator: "\r\n") + "\r\n\r\n"
            sendUDP(payload, to: config.multicastGroup, port: config.port)
        }
        log("已发送 NOTIFY \(nts) × \(targets.count)")
        onEvent(.notify(targets.count))
    }

    /// 周期重发 alive。
    ///
    /// 没有这个循环，设备只在启动那一刻被通告一次：
    /// 手机端缓存过期（max-age 到点）后再打开投屏列表，
    /// 设备列表就是空的，得重启 App 才能再看到 —— 症状很像「有时候搜不到」。
    private func startNotifyLoop() {
        let t = Thread { [weak self] in
            guard let self else { return }
            // 首次延迟 0.3s，避免和 HTTP 启动抢时间
            Thread.sleep(forTimeInterval: 0.3)
            while self.isRunning {
                self.announceAlive()
                // 按 1/2 周期切段睡，让 stop() 能较快退出
                var slept: TimeInterval = 0
                while slept < Self.notifyInterval && self.isRunning {
                    Thread.sleep(forTimeInterval: 1.0)
                    slept += 1.0
                }
            }
        }
        t.name = "ssdp.notify"
        t.qualityOfService = .utility
        t.start()
        notifyThread = t
    }

    private func sendUDP(_ text: String, to ip: String, port: UInt16) {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return }
        defer { close(fd) }

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr(ip)

        let bytes = Array(text.utf8)
        bytes.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            _ = withUnsafePointer(to: &addr) { addrPtr in
                addrPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sp in
                    sendto(fd, base, bytes.count, 0, sp, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
    }

    // MARK: - 工具

    static func extractHeader(_ text: String, key: String) -> String? {
        for line in text.components(separatedBy: "\r\n") {
            let parts = line.split(separator: ":", maxSplits: 1)
            guard parts.count == 2 else { continue }
            if parts[0].trimmingCharacters(in: .whitespaces).uppercased() == key.uppercased() {
                return parts[1].trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    static func ipString(_ addr: in_addr) -> String {
        var a = addr
        var buf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
        inet_ntop(AF_INET, &a, &buf, socklen_t(INET_ADDRSTRLEN))
        return String(cString: buf)
    }

    /// 取本机在局域网内的 IPv4 地址（排除回环、169.254 链路本地）
    /// 结果带 3 秒缓存：SOAP 请求会频繁调用，每次枚举接口开销太大。
    private static var cachedIP: (value: String, at: Date)?
    private static let ipCacheLock = NSLock()

    static func localIPAddress() -> String {
        ipCacheLock.lock()
        if let c = cachedIP, Date().timeIntervalSince(c.at) < 3 {
            ipCacheLock.unlock()
            return c.value
        }
        ipCacheLock.unlock()

        let ip = resolveLocalIP()

        ipCacheLock.lock()
        cachedIP = (ip, Date())
        ipCacheLock.unlock()

        return ip
    }

    private static func resolveLocalIP() -> String {
        var ifaddrPtr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddrPtr) == 0, let first = ifaddrPtr else { return "127.0.0.1" }
        defer { freeifaddrs(ifaddrPtr) }

        var candidates: [(name: String, ip: String)] = []
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = cursor {
            defer { cursor = cur.pointee.ifa_next }
            let ifa = cur.pointee
            guard let saPtr = ifa.ifa_addr, saPtr.pointee.sa_family == UInt8(AF_INET) else { continue }
            let flags = Int32(ifa.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0 else { continue }

            let sinAddr: in_addr = withUnsafePointer(to: saPtr.pointee) { raw -> in_addr in
                raw.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
            }
            let ip = ipString(sinAddr)
            if ip.hasPrefix("169.254.") { continue }
            candidates.append((String(cString: ifa.ifa_name), ip))
        }
        // 优先 en0（有线/主 WiFi），否则取第一个
        if let en0 = candidates.first(where: { $0.name == "en0" }) { return en0.ip }
        return candidates.first?.ip ?? "127.0.0.1"
    }

    enum SSDPError: LocalizedError {
        case socketCreate(Int32)
        case bindFailed(Int32)
        case interfaceEnum
        case multicastJoin

        var errorDescription: String? {
            switch self {
            case .socketCreate(let e): return "创建 socket 失败 (errno \(e))"
            case .bindFailed(let e): return "绑定 1900 端口失败 (errno \(e))，可能被其它 DLNA 服务占用"
            case .interfaceEnum: return "枚举网络接口失败"
            case .multicastJoin: return "加入组播组失败，检查本地网络权限"
            }
        }
    }
}