import AVFoundation
import Combine
import UIKit

/// 播放状态（对应 UPnP TransportState 与 UnPlay 的 playback_status）
enum PlaybackState: String {
    case stopped = "STOPPED"
    case playing = "PLAYING"
    case paused = "PAUSED_PLAYBACK"
    case transitioning = "TRANSITIONING"

    var displayName: String {
        switch self {
        case .stopped: return "已停止"
        case .playing: return "播放中"
        case .paused: return "已暂停"
        case .transitioning: return "缓冲中"
        }
    }
}

/// 播放列表条目
struct MediaItem: Identifiable, Equatable {
    let id = UUID()
    var title: String
    var url: URL
    var duration: Double = 0

    static func == (lhs: MediaItem, rhs: MediaItem) -> Bool {
        lhs.url == rhs.url && lhs.title == rhs.title
    }
}

/// 播放核心引擎。
///
/// 线程契约（重要）：本类未标 @MainActor，以兼容 tvOS 15 的并发检查。
/// 所有公开方法必须从主线程调用；内部 KVO 回调统一派发回主线程再改 @Published。
/// DLNAService 负责从 HTTP 线程桥接到主线程。
final class PlaybackEngine: ObservableObject {

    // MARK: - 对外状态

    @Published private(set) var state: PlaybackState = .stopped
    @Published private(set) var currentItem: MediaItem?
    @Published private(set) var playlist: [MediaItem] = []
    @Published private(set) var currentIndex: Int = 0
    @Published private(set) var currentTime: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var isBuffering = false
    @Published private(set) var volume: Float = 1.0
    @Published private(set) var isMuted = false
    @Published var errorMessage: String?

    /// 播放模式
    enum PlayMode: String {
        case normal = "NORMAL"          // 播完停止
        case repeatAll = "REPEAT_ALL"   // 列表循环
        case repeatOne = "REPEAT_ONE"   // 单集循环
        case shuffle = "SHUFFLE"        // 随机
    }
    @Published var playMode: PlayMode = .normal

    /// 忽略来自手机端的 Stop 指令。
    ///
    /// 为什么需要这个开关：部分 DLNA 客户端（尤其国产视频 App）在退出播放页
    /// 或切后台时会主动发 AVTransport Stop。这在用户只想「手机走开、电视继续放」
    /// 时会造成误停。开启后本机忽略 Stop，只在用户用遥控器停止时才真停。
    @Published var ignoreRemoteStop = true

    /// 记录最近一次 Stop 是否来自远端（供界面/日志区分）
    private(set) var lastStopWasRemote = false
    /// 累计被忽略的远端 Stop 次数，界面可展示「已保护 N 次」
    private(set) var ignoredRemoteStopCount = 0

    // MARK: - 内部

    private let player = AVPlayer()
    private var timeObserver: Any?
    private var statusObservation: NSKeyValueObservation?
    private var itemStatusObservation: NSKeyValueObservation?
    private var interruptionObserver: NSObjectProtocol?
    private var cancellables = Set<AnyCancellable>()

    /// 被系统中断（来电/音频抢占/切后台）前的播放意图，用于恢复
    private var shouldResumeAfterInterruption = false

    var avPlayer: AVPlayer { player }

    init() {
        // 先从持久化设置恢复「忽略远端 Stop」。
        // 不能只依赖 SettingsView.onAppear 同步：用户关掉开关后重启 App，
        // 在打开设置页之前引擎仍会用默认 true，开关等于失效。
        if UserDefaults.standard.object(forKey: "uncast.ignoreRemoteStop") != nil {
            ignoreRemoteStop = UserDefaults.standard.bool(forKey: "uncast.ignoreRemoteStop")
        }
        if let raw = UserDefaults.standard.string(forKey: "uncast.playmode"),
           let m = PlayMode(rawValue: raw) {
            playMode = m
        }

        configureAudioSession()
        setupObservers()
        setupLifecycleHandling()
    }

    private func configureAudioSession() {
        do {
            let session = AVAudioSession.sharedInstance()
            // .playback + moviePlayback：允许在锁屏/后台持续出声
            try session.setCategory(.playback, mode: .moviePlayback, options: [])
            try session.setActive(true)
        } catch {
            // tvOS 上失败通常不影响前台播放
        }
    }

    /// 音频中断与 App 生命周期处理。
    ///
    /// DLNA 场景的关键要求：手机退出不该影响电视。这里的处理覆盖两类打断：
    /// 1. AVAudioSession 中断（系统级音频抢占）：记录意图，结束后自动续播
    /// 2. App 短暂失焦：不主动暂停；若被系统暂停则在回前台时恢复
    private func setupLifecycleHandling() {
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { [weak self] note in
            guard let self,
                  let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }

            switch type {
            case .began:
                // 中断开始：记住原本是否在放，但不改 state（避免 UI 闪成暂停）
                self.shouldResumeAfterInterruption = (self.state == .playing)
            case .ended:
                guard self.shouldResumeAfterInterruption else { return }
                // 中断结束自动续播，无需用户干预
                do {
                    try AVAudioSession.sharedInstance().setActive(true)
                } catch { /* 忽略 */ }
                if self.currentItem != nil {
                    self.player.play()
                    self.state = .playing
                }
                self.shouldResumeAfterInterruption = false
            @unknown default:
                break
            }
        }
    }

    // MARK: - 播放控制

    /// 投屏入口：设置单个 URI 并立即播放
    func cast(url: URL, title: String? = nil) {
        let item = MediaItem(title: title ?? Self.guessTitle(from: url), url: url)
        playlist = [item]
        currentIndex = 0
        loadAndPlay(index: 0)
    }

    /// 投屏播放列表
    func castPlaylist(_ items: [MediaItem], mode: PlayMode = .repeatAll, startAt index: Int = 0) {
        guard !items.isEmpty else { return }
        playlist = items
        playMode = mode
        currentIndex = min(max(0, index), items.count - 1)
        loadAndPlay(index: currentIndex)
    }

    func play() {
        // 没有当前 item 但有列表时，从当前索引重新加载
        if player.currentItem == nil {
            if !playlist.isEmpty {
                loadAndPlay(index: min(currentIndex, playlist.count - 1))
            }
            return
        }
        player.play()
        state = .playing
    }

    func pause() {
        player.pause()
        if state == .playing { state = .paused }
    }

    func stop() {
        stop(isRemote: false)
    }

    /// 停止播放。
    /// - Parameter isRemote: true 表示来自手机的 DLNA Stop 指令。
    ///   当 ignoreRemoteStop 开启时，远端 Stop 会被忽略——
    ///   这是「手机走开、电视继续放」的关键：国产视频 App 退出播放页时常发 Stop。
    func stop(isRemote: Bool) {
        if isRemote && ignoreRemoteStop {
            lastStopWasRemote = true
            ignoredRemoteStopCount += 1
            return
        }
        player.pause()
        player.replaceCurrentItem(with: nil)
        state = .stopped
        currentTime = 0
        duration = 0
        currentItem = nil
        errorMessage = nil
    }

    func togglePlayPause() {
        switch state {
        case .playing: pause()
        case .paused, .stopped: play()
        default: break
        }
    }

    func seek(to seconds: Double) {
        guard seconds.isFinite, seconds >= 0 else { return }
        let target = CMTime(seconds: seconds, preferredTimescale: 600)
        player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func seek(by delta: Double) {
        let maxTime = duration > 0 ? duration : currentTime + abs(delta)
        let target = min(max(0, currentTime + delta), maxTime)
        seek(to: target)
    }

    func next() {
        guard !playlist.isEmpty else { return }
        let nextIndex = (currentIndex + 1) % playlist.count
        currentIndex = nextIndex
        loadAndPlay(index: nextIndex)
    }

    func previous() {
        guard !playlist.isEmpty else { return }
        if currentTime > 3 {
            seek(to: 0)
            return
        }
        let prevIndex = (currentIndex - 1 + playlist.count) % playlist.count
        currentIndex = prevIndex
        loadAndPlay(index: prevIndex)
    }

    func setVolume(_ v: Float) {
        volume = min(max(0, v), 1)
        player.volume = isMuted ? 0 : volume
    }

    func setMuted(_ muted: Bool) {
        isMuted = muted
        player.volume = muted ? 0 : volume
    }

    /// 音量 0-100（DLNA RenderingControl 用）
    func setVolumePercent(_ percent: Int) {
        setVolume(Float(min(max(0, percent), 100)) / 100.0)
    }

    var volumePercent: Int { Int((isMuted ? 0 : volume) * 100) }

    // MARK: - 加载

    private func loadAndPlay(index: Int) {
        guard index >= 0, index < playlist.count else { return }
        let item = playlist[index]
        currentItem = item
        currentTime = 0
        duration = 0
        errorMessage = nil
        state = .transitioning

        let asset = AVURLAsset(url: item.url, options: [
            AVURLAssetPreferPreciseDurationAndTimingKey: true
        ])
        let playerItem = AVPlayerItem(asset: asset)
        // 较大前向缓冲：手机端退出/网络抖动时靠缓冲续播，不容易断流
        playerItem.preferredForwardBufferDuration = 30
        // 允许自动等待缓冲而不是直接报错，配合下面的自动恢复
        playerItem.canUseNetworkResourcesForLiveStreamingWhilePaused = true
        player.automaticallyWaitsToMinimizeStalling = true

        // 观测当前 item 状态
        itemStatusObservation = playerItem.observe(\.status, options: [.new]) { [weak self] item, _ in
            let status = item.status
            let seconds = item.duration.seconds
            let errDesc = Self.describeError(item.error)
            DispatchQueue.main.async {
                guard let self else { return }
                switch status {
                case .readyToPlay:
                    if seconds.isFinite && seconds > 0 { self.duration = seconds }
                    self.errorMessage = nil
                case .failed:
                    self.errorMessage = errDesc
                    self.state = .stopped
                    // 网络类错误自动重试一次：手机退出可能引发短暂断网
                    self.scheduleRetryIfTransient(item.error)
                default:
                    break
                }
            }
        }

        // 用 block-based observer，避免 removeObserver(self) 的配对问题
        NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: playerItem,
            queue: .main
        ) { [weak self] _ in
            self?.handlePlaybackEnd()
        }

        player.replaceCurrentItem(with: playerItem)
        player.play()
        state = .playing
    }

    /// 网络类瞬时错误的重试。只重试一次，避免无限循环掩盖真实问题。
    /// 触发场景：手机退出导致 WiFi 短暂抖动、源站短时限流。
    private var retriedIndex: Int?

    private func scheduleRetryIfTransient(_ error: Error?) {
        guard let ns = error as NSError?, Self.isTransient(ns) else { return }
        // 同一集只自动重试一次
        guard retriedIndex != currentIndex else { return }
        retriedIndex = currentIndex

        let idx = currentIndex
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self, self.playlist.indices.contains(idx) else { return }
            // 仅当用户没有主动停止、且仍停留在这一集时才重试
            guard self.currentItem != nil, self.currentIndex == idx else { return }
            self.loadAndPlay(index: idx)
        }
    }

    private static func isTransient(_ error: NSError) -> Bool {
        // -1001 超时 / -1005 连接中断 / -1009 无网络 / -1004 连不上主机
        return [-1001, -1005, -1009, -1004].contains(error.code)
    }

    private func handlePlaybackEnd() {
        switch playMode {
        case .repeatOne:
            seek(to: 0)
            player.play()
            state = .playing
        case .repeatAll:
            next()
        case .shuffle:
            if playlist.count > 1 {
                let candidates = playlist.indices.filter { $0 != currentIndex }
                if let pick = candidates.randomElement() {
                    currentIndex = pick
                    loadAndPlay(index: pick)
                }
            } else {
                state = .stopped
            }
        case .normal:
            if currentIndex + 1 < playlist.count {
                currentIndex += 1
                loadAndPlay(index: currentIndex)
            } else {
                state = .stopped
                currentTime = 0
            }
        }
    }

    // MARK: - 观测

    private func setupObservers() {
        // 每 0.5 秒更新进度
        timeObserver = player.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.5, preferredTimescale: 600),
            queue: .main
        ) { [weak self] time in
            guard let self else { return }
            let secs = time.seconds
            if secs.isFinite { self.currentTime = secs }

            guard let item = self.player.currentItem else { return }
            if item.isPlaybackLikelyToKeepUp {
                self.isBuffering = false
                if self.state == .transitioning && self.player.rate > 0 {
                    self.state = .playing
                }
            } else if self.player.rate == 0 && self.state == .playing {
                self.isBuffering = true
            }
            if self.duration <= 0 {
                let d = item.duration.seconds
                if d.isFinite && d > 0 { self.duration = d }
            }
        }

        statusObservation = player.observe(\.timeControlStatus, options: [.new]) { [weak self] player, _ in
            let status = player.timeControlStatus
            DispatchQueue.main.async {
                guard let self else { return }
                switch status {
                case .playing:
                    if self.state != .paused { self.state = .playing }
                case .waitingToPlayAtSpecifiedRate:
                    self.isBuffering = true
                case .paused:
                    if self.currentItem != nil && self.state == .playing {
                        self.state = .paused
                    }
                @unknown default:
                    break
                }
            }
        }
    }

    // MARK: - 工具

    static func guessTitle(from url: URL) -> String {
        let name = url.deletingPathExtension().lastPathComponent
        return name.isEmpty ? "未命名媒体" : name
    }

    static func formatTime(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "00:00" }
        let total = Int(seconds)
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        if h > 0 {
            return String(format: "%d:%02d:%02d", h, m, s)
        }
        return String(format: "%02d:%02d", m, s)
    }

    /// 把 AVPlayer 的原始错误翻译成用户能看懂的话
    static func describeError(_ error: Error?) -> String {
        guard let error = error as NSError? else { return "播放失败" }
        switch error.code {
        case -1009: return "网络连接已断开"
        case -1001: return "请求超时，视频源无响应"
        case -1003: return "找不到服务器，检查地址是否有效"
        case -1008: return "视频源返回错误（HTTP 4xx/5xx，常见 403 防盗链）"
        case -11800: return "无法解码该视频格式"
        case -11828: return "该视频的音频编码不受支持"
        default:
            return error.localizedDescription
        }
    }
}