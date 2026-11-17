import Foundation
import Darwin

/// 极简 HTTP/1.1 服务端，基于 BSD socket。
///
/// 为什么不用 Network.framework / GCDWebServer：
/// - Network.framework 的 NWListener 在 tvOS 上对 raw HTTP 请求头大小/连接处理不够可控
/// - 引入第三方库会增加 CI 编译复杂度与体积
/// 手写 socket 反而最稳，代码量也不大。
///
/// 支持：
/// - GET /dd.xml            → UPnP device description
/// - GET /scpd/*.xml        → Service Control Protocol Description
/// - POST /upnp/control/*   → SOAP 控制（AVTransport / RenderingControl）
/// - GET /PlaybackEvent     → 兼容 UnPlay 的播放状态查询
/// - POST /                 → 兼容 UnPlay 的 HTTP 投屏指令
/// - GET /cast             → 内置网页投屏页面
final class HTTPServer {

    struct Request {
        var method: String = ""
        var path: String = ""
        var query: [String: String] = [:]
        var headers: [String: String] = [:]
        var body: Data = Data()
        var remoteIP: String = ""
    }

    struct Response {
        var status: Int = 200
        var headers: [String: String] = [:]
        var body: Data = Data()

        static func text(_ s: String, status: Int = 200, contentType: String = "text/plain; charset=utf-8") -> Response {
            var r = Response()
            r.status = status
            r.headers["Content-Type"] = contentType
            r.body = Data(s.utf8)
            return r
        }

        static func xml(_ s: String) -> Response {
            text(s, contentType: "text/xml; charset=utf-8")
        }

        static func json(_ s: String) -> Response {
            text(s, contentType: "application/json; charset=utf-8")
        }
    }

    typealias Handler = (Request) -> Response

    private let port: UInt16
    private var listenFD: Int32 = -1
    private var acceptThread: Thread?
    private var isRunning = false
    private var handler: Handler
    private let log: (String) -> Void

    init(port: UInt16, handler: @escaping Handler, log: @escaping (String) -> Void = { _ in }) {
        self.port = port
        self.handler = handler
        self.log = log
    }

    func start() throws {
        guard !isRunning else { return }

        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw HTTPError.socketCreate(errno) }

        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = INADDR_ANY

        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else {
            close(fd)
            throw HTTPError.bindFailed(errno)
        }

        guard listen(fd, 32) == 0 else {
            close(fd)
            throw HTTPError.listenFailed(errno)
        }

        listenFD = fd
        isRunning = true

        let t = Thread { [weak self] in self?.acceptLoop() }
        t.name = "http.accept"
        t.qualityOfService = .userInitiated
        t.start()
        acceptThread = t

        log("HTTP 服务启动 :\(port)")
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false
        if listenFD >= 0 {
            close(listenFD)
            listenFD = -1
        }
        acceptThread?.cancel()
        acceptThread = nil
    }

    private func acceptLoop() {
        while isRunning && listenFD >= 0 {
            var clientAddr = sockaddr_in()
            var len = socklen_t(MemoryLayout<sockaddr_in>.size)
            let clientFD = withUnsafeMutablePointer(to: &clientAddr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sp in
                    accept(listenFD, sp, &len)
                }
            }
            guard clientFD >= 0 else {
                if isRunning { Thread.sleep(forTimeInterval: 0.02) }
                continue
            }

            let ip = SSDPResponder.ipString(clientAddr.sin_addr)
            // 每连接一个线程：DLNA 请求量小，简单可靠优先
            let t = Thread { [weak self] in
                self?.handleConnection(fd: clientFD, remoteIP: ip)
            }
            t.qualityOfService = .userInitiated
            t.stackSize = 512 * 1024
            t.start()
        }
    }

    private func handleConnection(fd: Int32, remoteIP: String) {
        defer { close(fd) }
        // 读超时，防止半开连接吊死线程
        var tv = timeval(tv_sec: 10, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        guard var req = readRequest(fd: fd, remoteIP: remoteIP) else { return }
        let resp = handler(req)
        req.body = Data() // 释放
        writeResponse(fd: fd, resp: resp)
    }

    private func readRequest(fd: Int32, remoteIP: String) -> Request? {
        var buffer = Data()
        let chunkSize = 8192
        let chunk = UnsafeMutableRawPointer.allocate(byteCount: chunkSize, alignment: 1)
        defer { chunk.deallocate() }

        // 先读到 header 结束
        var headerEnd = -1
        while headerEnd < 0 {
            let n = recv(fd, chunk, chunkSize, 0)
            if n <= 0 { return nil }
            buffer.append(Data(bytes: chunk, count: n))
            headerEnd = Self.findHeaderEnd(buffer)
            if buffer.count > 64 * 1024 { return nil } // header 过大，拒绝
        }

        let headerData = buffer[0..<headerEnd]
        guard let headerText = String(data: headerData, encoding: .utf8) else { return nil }
        let lines = headerText.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { return nil }

        let parts = requestLine.split(separator: " ", maxSplits: 2)
        guard parts.count >= 2 else { return nil }

        var req = Request()
        req.method = String(parts[0]).uppercased()
        req.remoteIP = remoteIP

        let rawTarget = String(parts[1])
        if let qIndex = rawTarget.firstIndex(of: "?") {
            req.path = String(rawTarget[rawTarget.startIndex..<qIndex])
            let queryString = String(rawTarget[rawTarget.index(after: qIndex)...])
            req.query = Self.parseQuery(queryString)
        } else {
            req.path = rawTarget
        }

        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let key = String(line[line.startIndex..<colon]).trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            req.headers[key.lowercased()] = value
        }

        // body
        let contentLength = Int(req.headers["content-length"] ?? "0") ?? 0
        let bodyStart = headerEnd + 4
        if buffer.count > bodyStart {
            req.body = buffer.subdata(in: bodyStart..<buffer.count)
        }

        if contentLength > 0 {
            while req.body.count < contentLength {
                let n = recv(fd, chunk, chunkSize, 0)
                if n <= 0 { break }
                req.body.append(Data(bytes: chunk, count: n))
                if req.body.count > 32 * 1024 * 1024 { break } // 32MB 上限
            }
            if req.body.count > contentLength {
                req.body = req.body.subdata(in: 0..<contentLength)
            }
        }

        return req
    }

    private func writeResponse(fd: Int32, resp: Response) {
        var head = "HTTP/1.1 \(resp.status) \(Self.statusText(resp.status))\r\n"
        var headers = resp.headers
        headers["Content-Length"] = String(resp.body.count)
        headers["Connection"] = "close"
        // Server 头与 SSDP 的 SERVER 保持同一串。
        // 两处不一致时，同一台设备在 SSDP 响应和 HTTP 响应里自报
        // 不同的 UPnP 版本，严格实现的客户端会当成两台不同的设备。
        headers["Server"] = SSDPResponder.serverString
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "*"
        for (k, v) in headers {
            head += "\(k): \(v)\r\n"
        }
        head += "\r\n"

        var out = Data(head.utf8)
        out.append(resp.body)
        let total = out.count

        out.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < total {
                let n = send(fd, base.advanced(by: sent), total - sent, 0)
                if n <= 0 { break }
                sent += n
            }
        }
    }

    // MARK: - 解析工具

    private static func findHeaderEnd(_ data: Data) -> Int {
        // 找 \r\n\r\n
        let pattern: [UInt8] = [13, 10, 13, 10]
        guard data.count >= 4 else { return -1 }
        let bytes = [UInt8](data)
        for i in 0...(bytes.count - 4) {
            if bytes[i] == pattern[0], bytes[i+1] == pattern[1],
               bytes[i+2] == pattern[2], bytes[i+3] == pattern[3] {
                return i
            }
        }
        return -1
    }

    static func parseQuery(_ s: String) -> [String: String] {
        var result: [String: String] = [:]
        for pair in s.split(separator: "&") {
            let kv = pair.split(separator: "=", maxSplits: 1)
            guard kv.count == 2 else {
                if kv.count == 1 { result[String(kv[0]).removingPercentEncoding ?? String(kv[0])] = "" }
                continue
            }
            let k = String(kv[0]).replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? String(kv[0])
            let v = String(kv[1]).replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? String(kv[1])
            result[k] = v
        }
        return result
    }

    static func statusText(_ code: Int) -> String {
        switch code {
        case 200: return "OK"
        case 400: return "Bad Request"
        case 404: return "Not Found"
        case 405: return "Method Not Allowed"
        case 500: return "Internal Server Error"
        default: return "OK"
        }
    }

    enum HTTPError: LocalizedError {
        case socketCreate(Int32)
        case bindFailed(Int32)
        case listenFailed(Int32)

        var errorDescription: String? {
            switch self {
            case .socketCreate(let e): return "创建 HTTP socket 失败 (errno \(e))"
            case .bindFailed(let e): return "HTTP 端口绑定失败 (errno \(e))，可能被占用"
            case .listenFailed(let e): return "HTTP listen 失败 (errno \(e))"
            }
        }
    }
}