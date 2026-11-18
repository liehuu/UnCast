import Combine
import Foundation

/// DLNA 接收器总协调器。
///
/// 把 SSDP 发现、HTTP 服务、SOAP 控制、播放引擎串成一条链路：
///
///   [手机客户端] --SSDP M-SEARCH--> [SSDPResponder] --200 OK/LOCATION-->  [手机客户端]
///   [手机客户端] --GET /dd.xml--------> device description
///   [手机客户端] --POST SetAVTransportURI--> [SOAP] --> [PlaybackEngine.cast]
///   [手机客户端] --POST Play-----------------> [PlaybackEngine.play]
///   [手机客户端] --POST GetPositionInfo------> 当前进度
///
/// 线程契约：
/// - HTTP / SSDP 服务在后台 Thread 运行，回调进入本类
/// - 本类不标 @MainActor，因此任何线程都可访问
/// - 但 @Published 的状态更新一律通过 `onMain` 派发到主线程（SwiftUI requirement）
/// - 触碰 PlaybackEngine 的操作同样通过 `onMain` 同步/异步派发
final class DLNAService: ObservableObject {

    // MARK: - 对外状态（只在主线程写）

    @Published private(set) var isRunning = false
    @Published private(set) var statusMessage = "未启动"
    @Published private(set) var logs: [LogEntry] = []
    @Published private(set) var localIP = "—"
    @Published private(set) var castCount = 0

    // 投屏兼容性计数器。用途是让用户用一次观察区分两种「搜不到」：
    // 对方 App 根本不发 M-SEARCH（只走 AirPlay/私有协议）→ 计数恒为 0；
    // 对方发了但我们没被认出来 → 计数在涨却没出现在列表里。
    // 这两个现象的处理方式完全不同，没有计数器就只能靠猜。
    @Published private(set) var mSearchCount = 0
    @Published private(set) var replyCount = 0
    @Published private(set) var notifyCount = 0

    struct LogEntry: Identifiable {
        let id = UUID()
        let time: Date
        let message: String
    }

    let engine: PlaybackEngine
    let uuid: String
    let friendlyName: String

    static let httpPort: UInt16 = 9030
    static let ssdpPort: UInt16 = 1900

    private var ssdp: SSDPResponder?
    private var http: HTTPServer?

    /// GENA 事件订阅表。手机端订了才会收到「播完了」的通知，
    /// 收到通知才会推下一集 —— 连播链路的关键一环，见 EventSubscription 注释。
    private let events = EventSubscription()

    /// 上次已推送过的传输状态。只在**变化时**发事件，
    /// 否则每 0.5 秒的进度回调会把 NOTIFY 刷成洪水。
    private var lastNotifiedState: PlaybackState?
    private var lastNotifiedURI: String?

    /// 事件订阅的 Combine 凭据。不持有它，sink 立刻被释放，一条事件都发不出去。
    private var cancellables = Set<AnyCancellable>()

    /// 保护 start/stop 的并发调用（UI 与后台线程都可能触发）
    private let lifecycleLock = NSLock()
    private var _running = false

    init(engine: PlaybackEngine) {
        self.engine = engine

        // UUID 必须持久化：同一设备重启后保持同一个 UDN，
        // 否则手机投屏列表会出现多个同名设备
        let key = "uncast.device.uuid"
        if let saved = UserDefaults.standard.string(forKey: key), !saved.isEmpty {
            uuid = saved
        } else {
            let generated = UUID().uuidString
            UserDefaults.standard.set(generated, forKey: key)
            uuid = generated
        }

        let savedName = UserDefaults.standard.string(forKey: "uncast.device.name")
        friendlyName = (savedName?.isEmpty == false) ? savedName! : "UnCast (Apple TV)"

        // 播完立刻推事件。这是手机端能自动连播的唯一依据：
        // 它收到 STOPPED（或新一集的 PLAYING）才知道该推下一集。
        engine.onPlaybackFinished = { [weak self] in
            guard let self else { return }
            // 等一帧再发：handlePlaybackEnd 里 repeatAll 已经 loadAndPlay 新集，
            // 此时 state/currentItem 才是新值。立刻发会push 出旧的 URI。
            DispatchQueue.main.async {
                self.notifyStateNow()
            }
        }

        // state / currentItem 变化时推事件（内部按「值是否真的变了」去重，
        // 不会被 0.5 秒的进度回调刷成洪水）。
        //
        // 两个 publisher 分开订阅而不是 merge：$state 是 PlaybackState、
        // $currentItem 是 MediaItem?，类型不同，merge 需要先 map 成同一类型，
        // 而 map 出来的中间类型在 tvOS 15 的旧 Combine 上容易推不出来。
        // 分开订阅多两行，但没有类型风险。
        engine.$state
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.notifyIfStateChanged() }
            .store(in: &cancellables)

        engine.$currentItem
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.notifyIfStateChanged() }
            .store(in: &cancellables)
    }

    // MARK: - 主线程派发helper

    /// 若已在主线程则同步执行，否则切主线程同步执行。返回结果。
    private func onMainSync<T>(_ work: () -> T) -> T {
        if Thread.isMainThread { return work() }
        return DispatchQueue.main.sync(execute: work)
    }

    /// 异步派发到主线程（用于日志、状态刷新，不需要等待结果）
    private func onMain(_ work: @escaping () -> Void) {
        if Thread.isMainThread {
            work()
        } else {
            DispatchQueue.main.async(execute: work)
        }
    }

    // MARK: - 启停

    func start() {
        lifecycleLock.lock()
        guard !_running else {
            lifecycleLock.unlock()
            return
        }
        _running = true
        lifecycleLock.unlock()

        let ip = SSDPResponder.localIPAddress()

        let server = HTTPServer(port: Self.httpPort, handler: { [weak self] request in
            guard let self else { return HTTPServer.Response.text("", status: 500) }
            return self.route(request)
        }, log: { [weak self] msg in
            self?.appendLog(msg)
        })

        do {
            try server.start()
        } catch {
            let msg = "HTTP 启动失败：\(error.localizedDescription)"
            lifecycleLock.lock()
            _running = false
            lifecycleLock.unlock()
            onMain { [weak self] in
                self?.localIP = ip
                self?.statusMessage = msg
                self?.appendLogOnMain("❌ \(msg)")
            }
            return
        }
        lifecycleLock.lock()
        http = server
        lifecycleLock.unlock()

        let responder = SSDPResponder(config: .init(
            uuid: uuid,
            friendlyName: friendlyName,
            httpPort: Self.httpPort
        ), log: { [weak self] msg in
            self?.appendLog(msg)
        }, onEvent: { [weak self] event in
            // 回调本身可能来自 SSDP 后台线程；appendLogOnMain 会派发到主线程，
            // 这里只负责把事件翻译成计数，不直接写 @Published。
            guard let self else { return }
            switch event {
            case .search: self.bump(.search)
            case .reply(let n): self.bump(.reply, by: n)
            case .notify(let n): self.bump(.notify, by: n)
            }
        })

        do {
            try responder.start()
            lifecycleLock.lock()
            ssdp = responder
            lifecycleLock.unlock()
            // 启动通告由 SSDPResponder 内部的 notify 循环负责（含 0.3s 首次延迟），
            // 这里不再重复发一次 —— 重复发只会让局域网多收 5 条无意义报文。
        } catch {
            let msg = "SSDP 启动失败：\(error.localizedDescription)"
            lifecycleLock.lock()
            _running = false
            lifecycleLock.unlock()
            onMain { [weak self] in
                self?.localIP = ip
                self?.statusMessage = msg
                self?.appendLogOnMain("❌ \(msg)")
                self?.appendLogOnMain("提示：确认「本地网络」权限已授予，且 1900 端口未被 YouTube 等 App 占用")
            }
            server.stop()
            lifecycleLock.lock()
            http = nil
            lifecycleLock.unlock()
            return
        }

        onMain { [weak self] in
            guard let self else { return }
            self.localIP = ip
            self.isRunning = true
            self.statusMessage = "投屏服务运行中"
            self.appendLogOnMain("本机 IP: \(ip)")
            self.appendLogOnMain("✅ DLNA 接收器已启动，设备名「\(self.friendlyName)」")
        }
    }

    func stop() {
        lifecycleLock.lock()
        _running = false
        let ssdpRef = ssdp
        let httpRef = http
        ssdp = nil
        http = nil
        lifecycleLock.unlock()

        ssdpRef?.stop()
        httpRef?.stop()

        onMain { [weak self] in
            self?.isRunning = false
            self?.statusMessage = "已停止"
            self?.appendLogOnMain("服务已停止")
        }
    }

    func restart() {
        stop()
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.start()
        }
    }

    // MARK: - 路由（HTTP 后台线程调用）

    private func route(_ request: HTTPServer.Request) -> HTTPServer.Response {
        appendLog("\(request.method) \(request.path) ← \(request.remoteIP)")

        switch (request.method, request.path) {
        case ("GET", "/dd.xml"):
            let xml = UPnPDescription.deviceXML(
                uuid: uuid,
                friendlyName: friendlyName,
                baseURL: "http://\(SSDPResponder.localIPAddress()):\(Self.httpPort)"
            )
            return .xml(xml)

        case ("GET", "/"):
            return HTTPServer.Response.text(
                Self.castPageHTML(ip: SSDPResponder.localIPAddress(), port: Self.httpPort),
                contentType: "text/html; charset=utf-8"
            )

        case ("GET", let p) where p.hasPrefix("/scpd/"):
            let name = p.replacingOccurrences(of: "/scpd/", with: "")
                .replacingOccurrences(of: ".xml", with: "")
            guard let xml = UPnPDescription.scpdXML(service: name) else {
                return .text("Not Found", status: 404)
            }
            return .xml(xml)

        case ("POST", let p) where p.hasPrefix("/upnp/control/"):
            let serviceName = p.replacingOccurrences(of: "/upnp/control/", with: "")
            return handleSOAP(request, serviceHint: serviceName)

        case ("SUBSCRIBE", _):
            return handleSubscribe(request)

        case ("UNSUBSCRIBE", _):
            return handleUnsubscribe(request)

        case ("GET", "/PlaybackEvent"):
            return handlePlaybackEvent()

        case ("POST", "/"):
            return handleHTTPCast(request)

        case ("GET", "/api/status"):
            return handleStatusJSON()

        case ("OPTIONS", _):
            var resp = HTTPServer.Response()
            resp.headers["Allow"] = "GET, POST, OPTIONS"
            return resp

        default:
            return .text("Not Found", status: 404)
        }
    }

    // MARK: - GENA 事件订阅

    /// SUBSCRIBE 处理。
    ///
    /// 三种情况要分开：
    /// - 带 `SID` → 续订（客户端定期续，否则 1800 秒后失效）
    /// - 带 `CALLBACK` → 新订阅
    /// - 都不带 → 400，不是 200。旧实现无脑回 200 + 编造的 SID，
    ///   客户端会以为自己订上了，然后永远等不到通知。
    private func handleSubscribe(_ request: HTTPServer.Request) -> HTTPServer.Response {
        let timeout = EventSubscription.parseTimeout(request.headers["timeout"])
        var resp = HTTPServer.Response()
        resp.headers["TIMEOUT"] = "Second-\(timeout)"

        // 续订
        if let sid = request.headers["sid"], !sid.isEmpty {
            if events.renew(sid: sid, timeout: timeout) {
                resp.headers["SID"] = sid
                appendLogOnMain("🔔 续订事件 \(sid.prefix(13))")
                return resp
            }
            // 规范要求：未知 SID 回 412，让客户端重新订阅
            var bad = HTTPServer.Response()
            bad.status = 412
            bad.headers["Content-Type"] = "text/plain; charset=utf-8"
            return bad
        }

        // 新订阅
        guard let callback = EventSubscription.parseCallback(request.headers["callback"]) else {
            var bad = HTTPServer.Response()
            bad.status = 400
            bad.headers["Content-Type"] = "text/plain; charset=utf-8"
            return bad
        }

        // 订的是哪个服务，从 eventSubURL 路径里取：
        // /upnp/event/AVTransport → AVTransport
        // /upnp/event/RenderingControl → RenderingControl
        // 取不到就按 AVTransport 处理（绝大多数客户端只订这个）。
        let service = Self.serviceName(fromEventURL: request.path)
        let sid = events.subscribe(callback: callback, timeout: timeout, service: service)
        resp.headers["SID"] = sid
        appendLogOnMain("🔔 订阅事件 \(sid.prefix(13)) [\(service)] → \(callback.host ?? "?")")

        // 规范要求：新订阅后必须先推一次**当前全部状态**（初始事件），
        // 否则客户端拿不到基线，后续的增量事件它无从比较。
        //
        // 注意这里必须整体走 onMainSync 一次取全：分几次读会让
        // state / uri / duration 来自不同时刻，客户端算出不一致的基线。
        let snapshot = onMainSync { [weak self] () -> (String, String, Double, Int)? in
            guard let self else { return nil }
            return (self.engine.state.rawValue,
                    self.engine.currentItem?.url.absoluteString ?? "",
                    self.engine.duration,
                    self.engine.volumePercent)
        }
        if let (state, uri, dur, vol) = snapshot {
            sendStateEvent(state: state, trackURI: uri, duration: dur, volume: vol)
        }
        return resp
    }

    /// 从 eventSubURL 路径解析服务名。
    /// `/upnp/event/AVTransport` → `AVTransport`；取不到时按 AVTransport 兜底。
    static func serviceName(fromEventURL path: String) -> String {
        var name = path.replacingOccurrences(of: "/upnp/event/", with: "")
        // 去掉结尾的实例号（少数客户端把 eventSubURL 写成 AVTransport1）。
        // 只剥数字，不剥别的 —— "ConnectionManager" 结尾是 r，不受影响。
        while let last = name.last, last.isNumber {
            name.removeLast()
        }
        switch name {
        case "AVTransport", "RenderingControl", "ConnectionManager":
            return name
        default:
            return "AVTransport"
        }
    }

    private func handleUnsubscribe(_ request: HTTPServer.Request) -> HTTPServer.Response {
        var resp = HTTPServer.Response()
        if let sid = request.headers["sid"], events.unsubscribe(sid: sid) {
            appendLogOnMain("🔕 取消订阅 \(sid.prefix(13))")
            return resp
        }
        // 没有 SID 或订不存在：规范要求 412
        resp.status = 412
        return resp
    }

    /// 状态变化时推事件。**只在变化时**推 —— 被 0.5 秒的进度回调驱动，
    /// 不判断变化就会每秒刷两条 NOTIFY 把手机端淹掉。
    func notifyIfStateChanged() {
        let (state, uri) = onMainSync { [weak self] () -> (PlaybackState, String) in
            guard let self else { return (.stopped, "") }
            return (self.engine.state, self.engine.currentItem?.url.absoluteString ?? "")
        }
        guard state != lastNotifiedState || uri != lastNotifiedURI else { return }
        lastNotifiedState = state
        lastNotifiedURI = uri

        let (dur, vol) = onMainSync { [weak self] () -> (Double, Int) in
            guard let self else { return (0, 100) }
            return (self.engine.duration, self.engine.volumePercent)
        }
        sendStateEvent(state: state.rawValue, trackURI: uri, duration: dur, volume: vol)
    }

    /// 主动推一次当前状态（不判断是否变化）。
    /// 播完/切集这类**必须**让手机知道的事件走这里。
    func notifyStateNow() {
        let info = onMainSync { [weak self] () -> (String, String, Double, Int)? in
            guard let self else { return nil }
            return (self.engine.state.rawValue,
                    self.engine.currentItem?.url.absoluteString ?? "",
                    self.engine.duration,
                    self.engine.volumePercent)
        }
        guard let (state, uri, dur, vol) = info else { return }
        lastNotifiedState = PlaybackState(rawValue: state)
        lastNotifiedURI = uri
        sendStateEvent(state: state, trackURI: uri, duration: dur, volume: vol)
    }

    private func sendStateEvent(state: String, trackURI: String, duration: Double, volume: Int) {
        // playMode 必须在主线程读 —— PlaybackEngine 的线程契约是
        // 所有属性只在主线程访问（本类不标 @MainActor，编译器不会拦）。
        // 在后台闭包里直接读 engine.playMode 是隐性违规。
        let playMode = onMainSync { [weak self] () -> String in
            self?.engine.playMode.rawValue ?? "NORMAL"
        }
        let body = EventSubscription.lastChangeBody(
            transportState: state,
            currentTrackURI: trackURI,
            currentTrackDuration: Self.formatDuration(duration),
            currentPlayMode: playMode
        )
        let volumeBody = EventSubscription.volumeBody(volume: volume)
        let log: (String) -> Void = { [weak self] msg in self?.appendLogOnMain(msg) }
        // events 是 let 常量、内部全部走 NSLock，跨线程访问安全；
        // 把它先绑到局部变量，避免后台闭包捕获 self。
        let events = self.events
        DispatchQueue.global(qos: .utility).async {
            events.notify(body: body, service: "AVTransport", log: log)
            // RenderingControl 的 Volume 发给订了那个服务的客户端。
            // 不能发给 AVTransport 的订阅者：它解析 LastChange 时找不到
            // 自己关注的 TransportState，严格实现会判为格式错误。
            events.notify(body: volumeBody, service: "RenderingControl", log: log)
        }
    }

    // MARK: - SOAP

    private func handleSOAP(_ request: HTTPServer.Request, serviceHint: String) -> HTTPServer.Response {
        let soapAction = request.headers["soapaction"]
        guard let action = SOAP.parse(request.body, soapActionHeader: soapAction) else {
            return .text(SOAP.errorResponse(code: 401, description: "Invalid Action"),
                         status: 500, contentType: "text/xml; charset=utf-8")
        }

        let service = action.service.isEmpty
            ? "urn:schemas-upnp-org:service:\(serviceHint):1"
            : action.service

        appendLog("SOAP \(action.name)")

        // 引擎状态读写必须在主线程，SOAP 需要同步结果 → sync
        let responseArgs = onMainSync { [weak self] () -> [(String, String)] in
            self?.perform(action: action) ?? []
        }

        let body = SOAP.successResponse(service: service, action: action.name, args: responseArgs)
        return .text(body, contentType: "text/xml; charset=utf-8")
    }

    /// 各 service 的 action 实现。调用方保证在主线程。
    private func perform(action: SOAP.Action) -> [(String, String)] {
        let args = action.args

        switch action.name {

        // ---------- AVTransport ----------
        case "SetAVTransportURI":
            if let uriString = args["CurrentURI"],
               let url = URL(string: uriString),
               url.scheme != nil {
                let title = args["CurrentURIMetaData"].flatMap { SOAP.titleFromDIDL($0) }
                engine.cast(url: url, title: title)
                castCount += 1
                appendLogOnMain("📺 收到投屏: \(title ?? url.lastPathComponent)")
            } else {
                appendLogOnMain("⚠️ SetAVTransportURI 参数无效")
            }
            return []

        case "SetNextAVTransportURI":
            if let uriString = args["NextURI"], let url = URL(string: uriString), url.scheme != nil {
                let title = args["NextURIMetaData"].flatMap { SOAP.titleFromDIDL($0) }
                // 只入队，绝不重新加载当前项。
                // 旧实现调的是 castPlaylist(_:mode:startAt:)，它内部无条件
                // loadAndPlay(currentIndex)，会把正在播的这一集重置回 0:00。
                // 手机端在播放中预载下一集时，用户会看到画面突然跳回开头。
                engine.appendToQueue(url: url, title: title)
                // 入队后若队列里的下一集还没顶上来，保持原播放模式：
                // 单集模式（NORMAL）下播完仍应停止，不能因为预载了下集就变成循环。
                appendLogOnMain("➕ 已排入下一集: \(title ?? url.lastPathComponent)")
            }
            return []

        case "Play":
            engine.play()
            return []

        case "Pause":
            engine.pause()
            return []

        case "Stop":
            // 标记为远端 Stop：若 engine.ignoreRemoteStop 开启则会被忽略，
            // 避免手机退出播放页时把电视上的播放一起停掉
            engine.stop(isRemote: true)
            if engine.ignoreRemoteStop {
                appendLogOnMain("⏭ 已忽略手机端 Stop（配置：手机退出不影响电视）")
            }
            return []

        case "Seek":
            let unit = args["Unit"] ?? "REL_TIME"
            let target = args["Target"] ?? "0"
            if unit == "REL_TIME" {
                engine.seek(to: Self.parseTime(target))
            }
            return []

        case "GetTransportInfo":
            return [
                ("CurrentTransportState", engine.state.rawValue),
                ("CurrentTransportStatus", "OK"),
                ("CurrentSpeed", "1")
            ]

        case "GetPositionInfo":
            return [
                ("Track", "\(engine.currentIndex + 1)"),
                ("TrackDuration", Self.formatDuration(engine.duration)),
                ("TrackMetaData", ""),
                ("TrackURI", engine.currentItem?.url.absoluteString ?? ""),
                ("RelTime", Self.formatDuration(engine.currentTime)),
                ("AbsTime", Self.formatDuration(engine.currentTime)),
                ("RelCount", "2147483647"),
                ("AbsCount", "2147483647")
            ]

        case "GetTransportSettings":
            return [("PlayMode", engine.playMode.rawValue)]

        case "SetPlayMode":
            if let mode = args["NewPlayMode"], let m = PlaybackEngine.PlayMode(rawValue: mode) {
                engine.playMode = m
                appendLogOnMain("播放模式 → \(mode)")
            }
            return []

        case "GetMediaInfo":
            return [
                ("NrTracks", "\(max(1, engine.playlist.count))"),
                ("MediaDuration", Self.formatDuration(engine.duration)),
                ("CurrentURI", engine.currentItem?.url.absoluteString ?? ""),
                ("CurrentURIMetaData", ""),
                ("NextURI", ""),
                ("NextURIMetaData", ""),
                ("PlayMedium", "NETWORK"),
                ("RecordMedium", "NOT_IMPLEMENTED"),
                ("WriteStatus", "NOT_IMPLEMENTED")
            ]

        // ---------- RenderingControl ----------
        case "GetVolume":
            return [("CurrentVolume", "\(engine.volumePercent)")]

        case "SetVolume":
            if let v = args["DesiredVolume"], let percent = Int(v) {
                engine.setVolumePercent(percent)
            }
            return []

        case "GetMute":
            return [("CurrentMute", engine.isMuted ? "1" : "0")]

        case "SetMute":
            if let v = args["DesiredMute"] {
                engine.setMuted(v == "1" || v.lowercased() == "true")
            }
            return []

        // ---------- ConnectionManager ----------
        case "GetProtocolInfo":
            return [("Source", ""), ("Sink", UPnPDescription.sinkProtocolInfo)]

        case "GetCurrentConnectionIDs":
            return [("ConnectionIDs", "0")]

        case "GetCurrentConnectionInfo":
            return [
                ("RcsID", "0"),
                ("AVTransportID", "0"),
                ("ProtocolInfo", ""),
                ("PeerConnectionManager", ""),
                ("PeerConnectionID", "-1"),
                ("Direction", "Input"),
                ("Status", "OK")
            ]

        default:
            appendLogOnMain("⚠️ 未实现的动作: \(action.name)")
            return []
        }
    }

    // MARK: - UnPlay 兼容 API

    /// GET /PlaybackEvent —— 对齐 UnPlay 返回结构
    private func handlePlaybackEvent() -> HTTPServer.Response {
        let snapshot = onMainSync { [weak self] () -> [String: Any] in
            guard let self else { return [:] }
            return [
                "total_duration": self.engine.duration,
                "current_time": self.engine.currentTime,
                "playback_status": self.engine.state.rawValue,
                "title": self.engine.currentItem?.title ?? "",
                "current_index": self.engine.currentIndex,
                "total_videos": self.engine.playlist.count
            ]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else {
            return .json("{}")
        }
        return .json(json)
    }

    /// POST / —— 对齐 UnPlay 的 HTTP 投屏指令
    private func handleHTTPCast(_ request: HTTPServer.Request) -> HTTPServer.Response {
        var params: [String: String] = [:]
        let contentType = request.headers["content-type"]?.lowercased() ?? ""

        if contentType.contains("application/json") {
            if let obj = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any] {
                for (k, v) in obj { params[k] = "\(v)" }
            }
        } else {
            let bodyString = String(data: request.body, encoding: .utf8) ?? ""
            params = HTTPServer.parseQuery(bodyString)
        }

        guard let rawURLs = params["urls"], !rawURLs.isEmpty else {
            return Self.jsonResponse(["ok": false, "error": "缺少 urls 参数"], status: 400)
        }

        let items = Self.parsePlaylist(rawURLs)
        guard !items.isEmpty else {
            return Self.jsonResponse(["ok": false, "error": "urls 中没有有效地址"], status: 400)
        }

        let modeString = params["mode"] ?? "none"
        let mode: PlaybackEngine.PlayMode = (modeString == "playList") ? .repeatAll : .normal

        _ = onMainSync { [weak self] () -> Bool in
            guard let self else { return false }
            self.castCount += 1
            self.engine.castPlaylist(items, mode: mode, startAt: 0)
            self.appendLogOnMain("📲 HTTP 投屏 \(items.count) 条, mode=\(modeString)")
            return true
        }

        return Self.jsonResponse([
            "ok": true,
            "count": items.count,
            "mode": modeString,
            "option": params["option"] ?? "native"
        ])
    }

    private func handleStatusJSON() -> HTTPServer.Response {
        let payload = onMainSync { [weak self] () -> [String: Any] in
            guard let self else { return [:] }
            return [
                "running": self.isRunning,
                "ip": self.localIP,
                "port": Int(Self.httpPort),
                "device": self.friendlyName,
                "state": self.engine.state.rawValue,
                "title": self.engine.currentItem?.title ?? "",
                "current_time": self.engine.currentTime,
                "duration": self.engine.duration,
                "playlist_count": self.engine.playlist.count,
                "cast_count": self.castCount
            ]
        }
        return Self.jsonResponse(payload)
    }

    private static func jsonResponse(_ obj: [String: Any], status: Int = 200) -> HTTPServer.Response {
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else {
            return HTTPServer.Response.text("{}", status: status, contentType: "application/json")
        }
        return HTTPServer.Response.text(json, status: status, contentType: "application/json; charset=utf-8")
    }

    /// 解析 UnPlay 格式播放列表：每行 `标题$地址` 或 `标题,地址`，也可只有地址
    static func parsePlaylist(_ text: String) -> [MediaItem] {
        var items: [MediaItem] = []
        for line in text.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }

            var title: String?
            var urlString: String

            if let dollar = trimmed.firstIndex(of: "$") {
                title = String(trimmed[trimmed.startIndex..<dollar]).trimmingCharacters(in: .whitespaces)
                urlString = String(trimmed[trimmed.index(after: dollar)...]).trimmingCharacters(in: .whitespaces)
            } else if let comma = trimmed.firstIndex(of: ","),
                      trimmed[trimmed.index(after: comma)...].trimmingCharacters(in: .whitespaces).hasPrefix("http") {
                title = String(trimmed[trimmed.startIndex..<comma]).trimmingCharacters(in: .whitespaces)
                urlString = String(trimmed[trimmed.index(after: comma)...]).trimmingCharacters(in: .whitespaces)
            } else {
                urlString = trimmed
            }

            guard let url = URL(string: urlString),
                  let scheme = url.scheme?.lowercased(),
                  ["http", "https", "rtsp", "file"].contains(scheme) else { continue }

            let finalTitle = (title?.isEmpty == false) ? title! : PlaybackEngine.guessTitle(from: url)
            items.append(MediaItem(title: finalTitle, url: url))
        }
        return items
    }

    // MARK: - 日志

    /// 任意线程安全：统一在主线程改 @Published
    private func appendLog(_ message: String) {
        appendLogOnMain(message)
    }

    /// 计数器自增。
    ///
    /// 刻意不用 keyPath（`\DLNAService.mSearchCount`）省三行代码：
    /// 那需要把 @Published private(set) 的属性当成可写 keypath 传出去，
    /// 本机没有 Swift 工具链、无法本地验证能否编译。
    /// 这里宁可啰嗦，也不要引入一个只能在 CI 上才暴露的错误。
    private func bump(_ kind: Kind, by delta: Int = 1) {
        onMain { [weak self] in
            guard let self else { return }
            switch kind {
            case .search: self.mSearchCount += delta
            case .reply: self.replyCount += delta
            case .notify: self.notifyCount += delta
            }
        }
    }

    private enum Kind {
        case search, reply, notify
    }

    private func appendLogOnMain(_ message: String) {
        onMain { [weak self] in
            guard let self else { return }
            self.logs.append(LogEntry(time: Date(), message: message))
            if self.logs.count > 300 {
                self.logs.removeFirst(self.logs.count - 300)
            }
        }
    }

    // MARK: - 时间格式

    /// UPnP 时间格式 HH:MM:SS
    static func formatDuration(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "00:00:00" }
        let total = Int(seconds)
        return String(format: "%02d:%02d:%02d", total / 3600, (total % 3600) / 60, total % 60)
    }

    /// 解析 HH:MM:SS 或纯秒数
    static func parseTime(_ s: String) -> Double {
        if s.contains(":") {
            let parts = s.split(separator: ":").compactMap { Double($0) }
            if parts.count == 3 { return parts[0] * 3600 + parts[1] * 60 + parts[2] }
            if parts.count == 2 { return parts[0] * 60 + parts[1] }
        }
        return Double(s) ?? 0
    }

    // MARK: - 内置网页投屏页

    static func castPageHTML(ip: String, port: UInt16) -> String {
        """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>UnCast 投屏</title>
        <style>
          * { box-sizing: border-box; }
          body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 0; padding: 20px;
                 background: #f5f5f7; color: #1d1d1f; }
          .card { background: #fff; border-radius: 14px; padding: 20px; max-width: 640px; margin: 0 auto 16px;
                  box-shadow: 0 2px 12px rgba(0,0,0,.08); }
          h1 { font-size: 20px; margin: 0 0 4px; }
          .sub { color: #86868b; font-size: 13px; margin-bottom: 16px; }
          label { display: block; font-size: 13px; color: #6e6e73; margin: 12px 0 6px; }
          textarea { width: 100%; min-height: 140px; padding: 12px; border: 1px solid #d2d2d7;
                     border-radius: 10px; font-family: ui-monospace, Menlo, monospace; font-size: 13px;
                     resize: vertical; }
          select { width: 100%; padding: 10px 12px; border: 1px solid #d2d2d7; border-radius: 10px;
                   font-size: 14px; background: #fff; }
          button { width: 100%; margin-top: 16px; padding: 13px; border: 0; border-radius: 10px;
                   background: #0071e3; color: #fff; font-size: 16px; font-weight: 600; }
          button:active { background: #0062c4; }
          .hint { font-size: 12px; color: #86868b; margin-top: 8px; line-height: 1.6; }
          .status { margin-top: 14px; padding: 12px; border-radius: 10px; font-size: 13px; display: none; }
          .ok { background: #e8f9ee; color: #1a7f37; display: block; }
          .err { background: #fdeced; color: #c0392b; display: block; }
          code { background: #f5f5f7; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
        </style>
        </head>
        <body>
        <div class="card">
          <h1>UnCast 投屏</h1>
          <div class="sub">设备：Apple TV · \(ip):\(port)</div>

          <label>播放列表（每行一条，格式 <code>标题$地址</code>）</label>
          <textarea id="urls" placeholder="第1集$http://192.168.1.10/video1.mp4&#10;第2集$http://192.168.1.10/video2.mp4"></textarea>

          <label>播放模式</label>
          <select id="mode">
            <option value="none">只播放一集</option>
            <option value="playList" selected>列表循环</option>
          </select>

          <button onclick="cast()">开始投屏</button>
          <div id="status" class="status"></div>
          <div class="hint">
            提示：视频地址需电视能直接访问（同一局域网）。<br>
            也可直接调用接口：<code>POST http://\(ip):\(port)/</code>
          </div>
        </div>

        <div class="card">
          <h1 style="font-size:16px">当前状态</h1>
          <div id="now" class="sub" style="margin:12px 0 0">查询中…</div>
        </div>

        <script>
        async function cast() {
          const urls = document.getElementById('urls').value.trim();
          const mode = document.getElementById('mode').value;
          if (!urls) { show('请先填写播放地址', false); return; }
          try {
            const body = 'urls=' + encodeURIComponent(urls) + '&option=native&mode=' + mode;
            const res = await fetch('/', {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
              body
            });
            const data = await res.json();
            if (data.ok) show('已推送 ' + data.count + ' 个视频，电视开始播放', true);
            else show('失败：' + (data.error || '未知错误'), false);
          } catch (e) { show('请求失败：' + e.message, false); }
        }

        function show(msg, ok) {
          const box = document.getElementById('status');
          box.textContent = msg;
          box.className = 'status ' + (ok ? 'ok' : 'err');
        }

        async function poll() {
          try {
            const res = await fetch('/PlaybackEvent');
            const d = await res.json();
            const map = { PLAYING: '播放中', PAUSED_PLAYBACK: '已暂停', STOPPED: '已停止', TRANSITIONING: '缓冲中' };
            const el = document.getElementById('now');
            if (!d.title) { el.textContent = '暂无播放内容'; return; }
            el.innerHTML = (map[d.playback_status] || d.playback_status) + ' · ' + esc(d.title) +
              '<br>' + fmt(d.current_time) + ' / ' + fmt(d.total_duration) +
              ' · 第 ' + (d.current_index + 1) + '/' + d.total_videos + ' 集';
          } catch (e) {}
        }

        function fmt(s) {
          s = Math.max(0, Math.floor(s || 0));
          const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
          return (h>0 ? h+':'+String(m).padStart(2,'0') : String(m).padStart(2,'0')) + ':' + String(sec).padStart(2,'0');
        }
        function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

        setInterval(poll, 2000);
        poll();
        </script>
        </body>
        </html>
        """
    }
}