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
        var multicastGroup = "239.255.255.250"
        var port: UInt16 = 1900
        /// 本机对外提供 device description 的 HTTP 端口
        var httpPort: UInt16 = 9030
        /// 设备 UUID，应持久化以保证设备识别稳定
        var uuid: String
        var friendlyName: String
    }

    private let config: Config
    private var socketFD: Int32 = -1
    private var recvThread: Thread?
    private var notifyThread: Thread?
    private var isRunning = false
    private let log: (String) -> Void

    init(config: Config, log: @escaping (String) -> Void = { _ in }) {
        self.config = config
        self.log = log
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

        log("SSDP 监听中 239.255.255.250:\(config.port)")
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

            guard stMatches(st) else { continue }

            let target = Self.ipString(sender.sin_addr)
            let targetPort = UInt16(bigEndian: sender.sin_port)
            log("M-SEARCH from \(target):\(targetPort) ST=\(st.prefix(48))")

            respondUnicast(to: target, port: targetPort, st: st)
        }
    }

    private func stMatches(_ st: String) -> Bool {
        let s = st.lowercased()
        if s == "ssdp:all" { return true }
        if s == "upnp:rootdevice" { return true }
        if s.hasPrefix("urn:schemas-upnp-org:device:mediarenderer") { return true }
        if s.hasPrefix("urn:schemas-upnp-org:service:avtransport") { return true }
        if s.hasPrefix("urn:schemas-upnp-org:service:renderingcontrol") { return true }
        if s.hasPrefix("urn:schemas-upnp-org:service:connectionmanager") { return true }
        return false
    }

    /// 单播回复 M-SEARCH。必须发到请求来源的 IP:端口，不能往 1900 组播回。
    private func respondUnicast(to ip: String, port: UInt16, st: String) {
        let location = "http://\(Self.localIPAddress()):\(config.httpPort)/dd.xml"
        let target = st.isEmpty ? "urn:schemas-upnp-org:device:MediaRenderer:1" : st

        let lines = [
            "HTTP/1.1 200 OK",
            "CACHE-CONTROL: max-age=1800",
            "EXT:",
            "LOCATION: \(location)",
            "SERVER: UnCast/1.0 UPnP/1.0 DLNADOC/1.50",
            "ST: \(target)",
            "USN: uuid:\(config.uuid)::\(target)",
            "BOOTID.UPNP.ORG: 1",
            "CONFIGID.UPNP.ORG: 1"
        ]
        // SSDP 报文必须以空行（\r\n\r\n）结尾
        let payload = lines.joined(separator: "\r\n") + "\r\n\r\n"
        sendUDP(payload, to: ip, port: port)
    }

    private func sendNotify(nts: String) {
        let location = "http://\(Self.localIPAddress()):\(config.httpPort)/dd.xml"
        let targets = [
            "upnp:rootdevice",
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:service:AVTransport:1",
            "urn:schemas-upnp-org:service:RenderingControl:1",
            "urn:schemas-upnp-org:service:ConnectionManager:1"
        ]
        for nt in targets {
            let lines = [
                "NOTIFY * HTTP/1.1",
                "HOST: \(config.multicastGroup):\(config.port)",
                "CACHE-CONTROL: max-age=1800",
                "LOCATION: \(location)",
                "NT: \(nt)",
                "NTS: \(nts)",
                "SERVER: UnCast/1.0 UPnP/1.0 DLNADOC/1.50",
                "USN: uuid:\(config.uuid)::\(nt)",
                "BOOTID.UPNP.ORG: 1",
                "CONFIGID.UPNP.ORG: 1"
            ]
            let payload = lines.joined(separator: "\r\n") + "\r\n\r\n"
            sendUDP(payload, to: config.multicastGroup, port: config.port)
        }
        log("已发送 NOTIFY \(nts)")
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