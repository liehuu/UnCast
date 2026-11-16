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

    // MARK: - 内部

    private let player = AVPlayer()
    private var timeObserver: Any?
    private var statusObservation: NSKeyValueObservation?
    private var itemStatusObservation: NSKeyValueObservation?
    private var cancellables = Set<AnyCancellable>()

    var avPlayer: AVPlayer { player }

    init() {
        configureAudioSession()
        setupObservers()
    }

    private func configureAudioSession() {
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .moviePlayback)
            try session.setActive(true)
        } catch {
            // tvOS 上失败通常不影响播放，忽略
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
        playerItem.preferredForwardBufferDuration = 5

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