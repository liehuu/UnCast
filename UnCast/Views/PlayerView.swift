import SwiftUI
import AVFoundation
import AVKit
import UIKit

/// 播放器界面。
///
/// ## 交互模型（v3，方向键冲突的最终解法）
///
/// 不再让两种行为去"争"同一批按键，而是把它们做成**互斥的两个状态**：
///
/// - **看片态**（控制层隐藏）：左右 = ±15 秒；上下 = 呼出控制层。
/// - **选键态**（控制层已显示且焦点落在按钮上）：左右 = 在按钮间移动；下 = 收起。
///
/// 每个按键在任一时刻只有一种含义，不存在仲裁。
///
/// ## 前两版为什么失败
///
/// **v1**：`onMoveCommand` 无条件拦截左右并 `seek`，所以"把焦点移到暂停键"
/// 这个动作永远走不到 —— 按左右必然快进。
///
/// **v2**：加了 `focusInControls` 上报回调，但失焦分支写成
/// `if focusInControls { /* 保持 true */ }`，等于**永远清不掉**。
/// 焦点一旦进过按钮，左右键就再也不能 seek，`scheduleHide` 也被这个
/// guard 永久挡住 —— 比 v1 更糟。
///
/// **v3（本版）**：焦点状态改用 `@FocusState` 枚举，由 SwiftUI 焦点引擎
/// 自己维护；`isFocusInControls` 是纯派生计算属性，结构上不存在
/// "忘记清状态"的可能。控制层隐藏时 `focus` 被显式置 nil。
///
/// ## 控制层为什么会"再也调不出来"
///
/// v1/v2 里控制层隐藏后只有 Play/Pause 键能唤回，方向键全被 seek 吃掉。
/// 现在**任何**方向键都能唤回：左右是"seek 并呼出"，上下是"直接呼出"。
struct PlayerView: View {
    @EnvironmentObject private var engine: PlaybackEngine

    // MARK: - 焦点

    /// 控制层按钮。`nil` == 焦点不在控制层 == 看片态。
    enum Control: Hashable {
        case rewind, previous, playPause, next, forward, mute, info, close
    }

    @FocusState private var focus: Control?

    /// 纯派生，不存状态 —— 因此不可能残留成 true。
    private var isFocusInControls: Bool { focus != nil }

    // MARK: - 状态

    @State private var showControls = true
    @State private var showInspector = false
    @State private var hideTask: Task<Void, Never>?

    /// seek 提示（秒，正负表方向），0 表示隐藏
    @State private var seekHint: Int = 0
    @State private var seekHintTask: Task<Void, Never>?

    var body: some View {
        playerLayer
    }

    // MARK: - 主层

    private var playerLayer: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            // 点按视频区域 = 暂停并呼出控制层。
            //
            // 关键：控制层显示时必须关掉这个手势。tvOS 的 UITapGestureRecognizer
            // 设了 allowedPressTypes=[.select] 后会**无视焦点**接收按键——
            // 若不停用，按确定键会同时触发"按钮动作"和这个手势，
            // 表现为点一下暂停又立刻恢复（看起来像没反应）。
            VideoLayerView(player: engine.avPlayer,
                           tapEnabled: !showControls,
                           onTap: { pauseAndReveal() })
                .ignoresSafeArea()

            if engine.isBuffering {
                ProgressView()
                    .progressViewStyle(.circular)
                    .scaleEffect(2.0)
                    .tint(.white)
            }

            if let error = engine.errorMessage {
                errorOverlay(error)
            }

            if seekHint != 0 {
                seekHintOverlay
                    .transition(.opacity)
                    .allowsHitTesting(false)
            }

            if showControls {
                controlsOverlay
                    .transition(.opacity)
            }

            if showInspector {
                inspectorOverlay
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: showControls)
        .animation(.easeInOut(duration: 0.18), value: showInspector)
        .animation(.easeInOut(duration: 0.15), value: seekHint)
        .onPlayPauseCommand {
            // 播放/暂停键永远可用，并顺手呼出控制层、把焦点送到播放键
            engine.togglePlayPause()
            revealControls(focusPlay: true)
        }
        .onMoveCommand(perform: handleMove)
        .onExitCommand(perform: handleExit)
        .onChange(of: showControls) { visible in
            // 控制层收起时确保焦点已释放，否则"看片态"还会以为焦点在按钮上
            if !visible { focus = nil }
        }
        .onDisappear {
            hideTask?.cancel()
            seekHintTask?.cancel()
        }
    }

    // MARK: - 方向键

    private func handleMove(_ direction: MoveCommandDirection) {
        // 检查器打开时把方向键让给它自己的焦点处理
        if showInspector { return }

        // ── 选键态 ──
        // 左右/上交给焦点引擎做按钮间移动，这里绝不 seek。
        // 只接管「下」：收起控制层，回到看片态。
        if isFocusInControls {
            if direction == .down { collapseControls() }
            return
        }

        // ── 看片态 ──
        // 这是"控制层一旦隐藏就调不出来"的解药：任何方向键都能唤回
        switch direction {
        case .left:
            seek(-15)
        case .right:
            seek(15)
        case .up, .down:
            revealControls(focusPlay: true)
        @unknown default:
            break
        }
    }

    private func handleExit() {
        if showInspector {
            withAnimation { showInspector = false }
            return
        }
        // 第一次按返回只收控制层，再按一次才退出播放
        if showControls {
            collapseControls()
        } else {
            engine.stop()
        }
    }

    private func seek(_ delta: Double) {
        engine.seek(by: delta)
        showSeekHint(Int(delta))
        // 不抢焦点：保持看片态，方便连按快进
        revealControls(focusPlay: false)
    }

    // MARK: - 控制层

    private var controlsOverlay: some View {
        VStack {
            topBar
            Spacer()
            bottomBar
        }
    }

    private var topBar: some View {
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
                Label("已保护 \(engine.ignoredRemoteStopCount) 次",
                      systemImage: "shield.checkered")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(.green.opacity(0.85))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.green.opacity(0.14), in: Capsule())
            }
        }
        .padding(.horizontal, 70)
        .padding(.top, 50)
        .background(alignment: .top) {
            LinearGradient(colors: [.black.opacity(0.75), .clear],
                           startPoint: .top, endPoint: .bottom)
                .frame(height: 220)
                .ignoresSafeArea(edges: .top)
        }
    }

    private var bottomBar: some View {
        VStack(spacing: 22) {
            progressBar
            controlButtons
        }
        .padding(.horizontal, 70)
        .padding(.bottom, 50)
        .background(alignment: .bottom) {
            LinearGradient(colors: [.clear, .black.opacity(0.85)],
                           startPoint: .top, endPoint: .bottom)
                .frame(height: 280)
                .ignoresSafeArea(edges: .bottom)
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
                let width = geo.size.width

                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Color.white.opacity(0.22))
                        .frame(height: 6)

                    Capsule()
                        .fill(Color.white)
                        .frame(width: max(0, width * progress), height: 6)

                    Circle()
                        .fill(Color.white)
                        .frame(width: 14, height: 14)
                        .offset(x: max(0, min(width - 14, width * progress - 7)))
                }
                .frame(maxHeight: .infinity, alignment: .center)
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
        HStack(spacing: 26) {
            CircleButton(icon: "gobackward.15", size: 26,
                         focus: $focus, tag: .rewind) {
                engine.seek(by: -15); showSeekHint(-15); revealControls(focusPlay: true)
            }

            CircleButton(icon: "backward.end.fill", size: 26,
                         focus: $focus, tag: .previous) {
                engine.previous(); revealControls(focusPlay: true)
            }

            CircleButton(icon: engine.state == .playing ? "pause.fill" : "play.fill",
                         size: 40, prominent: true,
                         focus: $focus, tag: .playPause) {
                engine.togglePlayPause(); revealControls(focusPlay: true)
            }

            CircleButton(icon: "forward.end.fill", size: 26,
                         focus: $focus, tag: .next) {
                engine.next(); revealControls(focusPlay: true)
            }

            CircleButton(icon: "goforward.15", size: 26,
                         focus: $focus, tag: .forward) {
                engine.seek(by: 15); showSeekHint(15); revealControls(focusPlay: true)
            }

            Spacer()

            CircleButton(icon: engine.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill",
                         size: 22, focus: $focus, tag: .mute) {
                engine.setMuted(!engine.isMuted); revealControls(focusPlay: true)
            }

            CircleButton(icon: "info.circle", size: 22,
                         focus: $focus, tag: .info) {
                withAnimation { showInspector.toggle() }; revealControls(focusPlay: true)
            }

            CircleButton(icon: "xmark", size: 22,
                         focus: $focus, tag: .close) {
                engine.stop()
            }
        }
        .focusSection()
    }

    // MARK: - seek 提示

    private var seekHintOverlay: some View {
        HStack(spacing: 10) {
            Image(systemName: seekHint > 0 ? "goforward.15" : "gobackward.15")
                .font(.system(size: 40, weight: .semibold))
            Text(seekHint > 0 ? "+\(seekHint)s" : "\(seekHint)s")
                .font(.system(size: 32, weight: .bold, design: .rounded))
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 34)
        .padding(.vertical, 20)
        .background(Color.black.opacity(0.55), in: Capsule())
    }

    private func showSeekHint(_ seconds: Int) {
        seekHint = seconds
        seekHintTask?.cancel()
        // 用 @MainActor 隔离的 Task，而不是 Task + MainActor.run：
        // 后者会在后台闭包里捕获 self（含 @FocusState），严格并发下告警。
        seekHintTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard !Task.isCancelled else { return }
            withAnimation { seekHint = 0 }
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

                if let size = engine.avPlayer.currentItem?.presentationSize,
                   size.width > 0 {
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

    // MARK: - 显示 / 隐藏

    /// 点按视频区域：暂停并呼出控制层
    private func pauseAndReveal() {
        if engine.state == .playing {
            engine.togglePlayPause()
        }
        revealControls(focusPlay: true)
    }

    /// 收起控制层，回到看片态（左右键恢复为 seek）
    private func collapseControls() {
        hideTask?.cancel()
        hideTask = nil
        // 先释放焦点再收层，避免层消失瞬间焦点引擎去抢别的元素
        focus = nil
        withAnimation { showControls = false }
    }

    private func revealControls(focusPlay: Bool) {
        withAnimation { showControls = true }
        scheduleHide()

        guard focusPlay else { return }
        // 等控制层真正上屏后再落焦点，否则按钮还不存在
        DispatchQueue.main.async {
            DispatchQueue.main.async {
                guard showControls, !showInspector else { return }
                focus = .playPause
            }
        }
    }

    /// 自动隐藏。两条保护：
    /// 1. 焦点在按钮上时不隐藏（用户正在选按钮）
    /// 2. 暂停 / 缓冲时不隐藏（此时用户大概率想操作）
    private func scheduleHide() {
        hideTask?.cancel()
        hideTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard !Task.isCancelled else { return }
            guard engine.state == .playing,
                  focus == nil,
                  !showInspector else { return }
            withAnimation { showControls = false }
        }
    }
}

// MARK: - 可聚焦圆形按钮

extension PlayerView {
    /// 圆形按钮。
    ///
    /// 焦点**不**通过回调上报，而是直接绑到父视图的 `@FocusState` 枚举。
    /// 焦点引擎自己维护那份状态，结构上不存在"上报了 true 忘了上报 false"。
    struct CircleButton: View {
        let icon: String
        let size: CGFloat
        var prominent: Bool = false
        let focus: FocusState<PlayerView.Control?>.Binding
        let tag: PlayerView.Control
        let action: () -> Void

        private var isFocused: Bool { focus.wrappedValue == tag }

        var body: some View {
            Button(action: action) {
                Image(systemName: icon)
                    .font(.system(size: size, weight: .medium))
                    .foregroundStyle(isFocused ? Color.black : Color.white)
                    .frame(width: prominent ? 84 : 60, height: prominent ? 84 : 60)
                    .background(
                        Circle().fill(isFocused ? Color.white : Color.white.opacity(0.16))
                    )
                    .overlay(
                        Circle().stroke(Color.white.opacity(isFocused ? 0.0 : 0.12),
                                        lineWidth: 2)
                    )
                    .scaleEffect(isFocused ? 1.12 : 1.0)
                    .animation(.easeOut(duration: 0.15), value: isFocused)
            }
            .buttonStyle(.plain)
            .focused(focus, equals: tag)
        }
    }
}

// MARK: - AVPlayerLayer 桥接

/// SwiftUI 在 tvOS 上没有原生 AVPlayerLayer 视图，必须用 UIViewRepresentable 桥接。
///
/// 点按识别也放在 UIKit 这一层：
/// tvOS 15 的 SwiftUI 没有 `onTapGesture` 的点击语义，而给 SwiftUI 视图加
/// `.focusable(true)` 又会和焦点引擎抢焦点。UIKit 手势识别器不参与焦点系统，
/// 是这里唯一干净的做法。
struct VideoLayerView: UIViewRepresentable {
    let player: AVPlayer
    /// 仅在控制层隐藏（看片态）时为 true。控制层显示时必须为 false，
    /// 否则确定键会绕过焦点引擎被手势抢走，按钮点一下等于点两下。
    var tapEnabled: Bool = false
    var onTap: (() -> Void)?

    func makeUIView(context: Context) -> PlayerContainerView {
        let view = PlayerContainerView()
        view.backgroundColor = .black
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspect
        view.isUserInteractionEnabled = true

        let tap = UITapGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.handleTap)
        )
        tap.allowedPressTypes = [NSNumber(value: UIPress.PressType.select.rawValue)]
        tap.isEnabled = tapEnabled
        view.addGestureRecognizer(tap)
        context.coordinator.tapGesture = tap
        return view
    }

    func updateUIView(_ uiView: PlayerContainerView, context: Context) {
        if uiView.playerLayer.player !== player {
            uiView.playerLayer.player = player
        }
        context.coordinator.onTap = onTap
        context.coordinator.tapGesture?.isEnabled = tapEnabled
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(onTap: onTap)
    }

    final class Coordinator: NSObject {
        var onTap: (() -> Void)?
        weak var tapGesture: UITapGestureRecognizer?

        init(onTap: (() -> Void)?) { self.onTap = onTap }

        @objc func handleTap(_ sender: UITapGestureRecognizer) {
            onTap?()
        }
    }
}

final class PlayerContainerView: UIView {
    override static var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
}