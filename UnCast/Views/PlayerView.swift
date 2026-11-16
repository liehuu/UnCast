import SwiftUI
import AVFoundation
import AVKit

/// 播放器界面。
///
/// tvOS 交互要点：
/// - 遥控器触控板点击 = 显示/隐藏控制层
/// - 左右滑动 = 快进/快退（我们做成分段 seek，比连续 seek 更省资源）
/// - 上滑 = 显示信息面板（对齐 UnPlay 的「下滑打开检查器」）
/// - Menu 键 = 返回首页（由系统处理）
struct PlayerView: View {
    @EnvironmentObject private var engine: PlaybackEngine
    @State private var showControls = true
    @State private var showInspector = false
    @State private var hideTask: Task<Void, Never>?

    var body: some View {
        // onTapGesture 在 tvOS 上要求 16.0+（Siri Remote 的点击映射为 tap 是 16 才开放的）。
        // 部署目标 15.0，因此做可用性分支：16+ 支持点击切换控制层，
        // 15 回退为仅靠 Play/Pause、Menu、左右滑动操作（这三者在 15 上均可用）。
        if #available(tvOS 16.0, *) {
            playerLayer
                .onTapGesture {
                    withAnimation { showControls.toggle() }
                    if showControls { scheduleHide() }
                }
        } else {
            playerLayer
        }
    }

    private var playerLayer: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            // 视频层
            VideoLayerView(player: engine.avPlayer)
                .ignoresSafeArea()

            // 缓冲指示
            if engine.isBuffering {
                ProgressView()
                    .progressViewStyle(.circular)
                    .scaleEffect(2.0)
                    .tint(.white)
            }

            // 错误提示
            if let error = engine.errorMessage {
                errorOverlay(error)
            }

            // 控制层
            if showControls {
                controlsOverlay
                    .transition(.opacity)
            }

            // 检查器
            if showInspector {
                inspectorOverlay
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: showControls)
        .animation(.easeInOut(duration: 0.25), value: showInspector)
        .onPlayPauseCommand {
            engine.togglePlayPause()
            revealControls()
        }
        .onMoveCommand { direction in
            switch direction {
            case .left:
                engine.seek(by: -15)
                revealControls()
            case .right:
                engine.seek(by: 15)
                revealControls()
            case .up:
                showInspector = true
            case .down:
                showControls.toggle()
            default:
                break
            }
        }
        .onExitCommand {
            if showInspector {
                showInspector = false
            } else if showControls {
                showControls = false
            } else {
                engine.stop()
            }
        }
    }

    // MARK: - 控制层

    private var controlsOverlay: some View {
        VStack {
            // 顶部：标题
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text(engine.currentItem?.title ?? "未知媒体")
                        .font(.system(size: 34, weight: .semibold))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                    if engine.playlist.count > 1 {
                        Text("第 \(engine.currentIndex + 1) / \(engine.playlist.count) 集")
                            .font(.system(size: 20))
                            .foregroundStyle(.white.opacity(0.6))
                    }
                }
                Spacer()
                if engine.playMode != .normal {
                    Text(modeLabel)
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(.white.opacity(0.7))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 6)
                        .background(Color.white.opacity(0.14), in: Capsule())
                }
                if engine.ignoreRemoteStop && engine.ignoredRemoteStopCount > 0 {
                    Label("已保护 \(engine.ignoredRemoteStopCount) 次", systemImage: "shield.checkered")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(.green.opacity(0.85))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(Color.green.opacity(0.14), in: Capsule())
                }
            }
            .padding(.horizontal, 70)
            .padding(.top, 50)
            .background(
                LinearGradient(colors: [.black.opacity(0.75), .clear],
                               startPoint: .top, endPoint: .bottom)
                    .frame(height: 220)
                    .ignoresSafeArea(edges: .top)
                , alignment: .top
            )

            Spacer()

            // 底部：进度 + 按钮
            VStack(spacing: 22) {
                progressBar
                controlButtons
            }
            .padding(.horizontal, 70)
            .padding(.bottom, 50)
            .background(
                LinearGradient(colors: [.clear, .black.opacity(0.85)],
                               startPoint: .top, endPoint: .bottom)
                    .frame(height: 260)
                    .ignoresSafeArea(edges: .bottom)
                , alignment: .bottom
            )
        }
    }

    private var modeLabel: String {
        switch engine.playMode {
        case .normal: return "顺序播放"
        case .repeatAll: return "列表循环"
        case .repeatOne: return "单集循环"
        case .shuffle: return "随机播放"
        }
    }

    // MARK: - 进度条

    private var progressBar: some View {
        VStack(spacing: 8) {
            GeometryReader { geo in
                let total = max(engine.duration, 1)
                let progress = min(max(engine.currentTime / total, 0), 1)

                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Color.white.opacity(0.22))
                        .frame(height: 6)

                    Capsule()
                        .fill(Color.white)
                        .frame(width: geo.size.width * progress, height: 6)

                    Circle()
                        .fill(Color.white)
                        .frame(width: 14, height: 14)
                        .offset(x: geo.size.width * progress - 7)
                }
            }
            .frame(height: 14)

            HStack {
                Text(PlaybackEngine.formatTime(engine.currentTime))
                Spacer()
                Text(PlaybackEngine.formatTime(engine.duration))
            }
            .font(.system(size: 18, weight: .medium, design: .monospaced))
            .foregroundStyle(.white.opacity(0.7))
        }
    }

    // MARK: - 按钮行

    private var controlButtons: some View {
        HStack(spacing: 30) {
            CircleButton(icon: "gobackward.15", size: 30) { engine.seek(by: -15) }
            CircleButton(icon: "backward.end.fill", size: 28) { engine.previous() }
            CircleButton(
                icon: engine.state == .playing ? "pause.fill" : "play.fill",
                size: 44,
                prominent: true
            ) { engine.togglePlayPause() }
            CircleButton(icon: "forward.end.fill", size: 28) { engine.next() }
            CircleButton(icon: "goforward.15", size: 30) { engine.seek(by: 15) }

            Spacer()

            CircleButton(icon: engine.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill", size: 24) {
                engine.setMuted(!engine.isMuted)
            }
            CircleButton(icon: "info.circle", size: 24) {
                withAnimation { showInspector.toggle() }
            }
            CircleButton(icon: "xmark", size: 24) { engine.stop() }
        }
    }

    // MARK: - 检查器

    private var inspectorOverlay: some View {
        HStack {
            Spacer()
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text("检查器")
                        .font(.system(size: 26, weight: .semibold))
                        .foregroundStyle(.white)
                    Spacer()
                    Button {
                        withAnimation { showInspector = false }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 24))
                            .foregroundStyle(.white.opacity(0.6))
                    }
                    .buttonStyle(.plain)
                }

                Divider().background(Color.white.opacity(0.18))

                inspectorRow("状态", engine.state.displayName)
                inspectorRow("当前时间", PlaybackEngine.formatTime(engine.currentTime))
                inspectorRow("总时长", PlaybackEngine.formatTime(engine.duration))
                inspectorRow("播放模式", modeLabel)
                inspectorRow("列表", "\(engine.currentIndex + 1) / \(engine.playlist.count)")
                inspectorRow("音量", "\(engine.volumePercent)%")

                if let item = engine.currentItem {
                    Divider().background(Color.white.opacity(0.18))
                    inspectorRow("主机", item.url.host ?? "—")
                    inspectorRow("协议", item.url.scheme?.uppercased() ?? "—")
                    Text(item.url.absoluteString)
                        .font(.system(size: 13, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.45))
                        .lineLimit(3)
                        .padding(.top, 4)
                }

                if let size = engine.avPlayer.currentItem?.presentationSize, size.width > 0 {
                    Divider().background(Color.white.opacity(0.18))
                    inspectorRow("分辨率", "\(Int(size.width)) × \(Int(size.height))")
                }
            }
            .padding(34)
            .frame(width: 460, alignment: .leading)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
            .padding(.trailing, 70)
        }
        .padding(.vertical, 90)
    }

    private func inspectorRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .font(.system(size: 18))
                .foregroundStyle(.white.opacity(0.55))
            Spacer()
            Text(value)
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(.white)
        }
    }

    // MARK: - 错误层

    private func errorOverlay(_ message: String) -> some View {
        VStack(spacing: 18) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 52))
                .foregroundStyle(.orange)
            Text(message)
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 800)
            Button("返回") { engine.stop() }
                .padding(.top, 8)
        }
        .padding(50)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24))
    }

    // MARK: - 自动隐藏

    private func revealControls() {
        withAnimation { showControls = true }
        scheduleHide()
    }

    private func scheduleHide() {
        hideTask?.cancel()
        hideTask = Task {
            try? await Task.sleep(nanoseconds: 4_500_000_000)
            guard !Task.isCancelled else { return }
            await MainActor.run {
                if engine.state == .playing {
                    withAnimation { showControls = false }
                }
            }
        }
    }
}

// MARK: - AVPlayerLayer 桥接

/// SwiftUI 在 tvOS 上没有原生 AVPlayerLayer 视图，必须用 UIViewRepresentable 桥接。
/// 这是 tvOS 视频播放的标准做法。
struct VideoLayerView: UIViewRepresentable {
    let player: AVPlayer

    func makeUIView(context: Context) -> PlayerContainerView {
        let view = PlayerContainerView()
        view.backgroundColor = .black
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspect
        return view
    }

    func updateUIView(_ uiView: PlayerContainerView, context: Context) {
        if uiView.playerLayer.player !== player {
            uiView.playerLayer.player = player
        }
    }
}

final class PlayerContainerView: UIView {
    override static var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
}

// MARK: - 圆形按钮

struct CircleButton: View {
    let icon: String
    let size: CGFloat
    var prominent: Bool = false
    let action: () -> Void
    @FocusState private var focused: Bool

    var body: some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: size, weight: .medium))
                .foregroundStyle(focused ? Color.black : Color.white)
                .frame(width: prominent ? 88 : 64, height: prominent ? 88 : 64)
                .background(
                    Circle().fill(focused ? Color.white : Color.white.opacity(0.16))
                )
                .scaleEffect(focused ? 1.12 : 1.0)
                .animation(.easeOut(duration: 0.15), value: focused)
        }
        .buttonStyle(.plain)
        .focused($focused)
    }
}