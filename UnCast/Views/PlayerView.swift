import SwiftUI
import AVFoundation
import AVKit
import UIKit

/// 播放器界面。
///
/// ## 交互模型（v4：三级累进控制层）
///
/// 每一级只做一件事，按键含义不重叠：
///
/// | 层级 | 屏幕内容 | 上 / 下 | 左 / 右 |
/// |---|---|---|---|
/// | `.hidden`（看片态） | 仅视频 | **下**：呼出进度条；**上**：无动作 | 快退/快进 15 秒 |
/// | `.progress`（进度条态） | 进度条 + 时间 | **下**：展开按钮行；**上**：收起 | 快退/快进 15 秒 |
/// | `.buttons`（按钮态） | 进度条 + 按钮行 | **下**：收起；**上**：交给焦点引擎 | 按钮间移动 |
///
/// ## v3 为什么"控制层一旦自动隐藏就再也调不出来"
///
/// v3 把 `onMoveCommand` 留在 SwiftUI 里，而它**依赖焦点**。
/// 控制层隐藏时 `focus = nil`，视图树里没有任何可聚焦元素，
/// tvOS 焦点引擎无处派发，`onMoveCommand` 一次都不会被调用 ——
/// 于是 `handleMove` 里的 `case .up, .down: revealControls()` 成了死代码。
///
/// 这个缺陷**读代码看不出来**：分支明明写着"上下键呼出控制层"，
/// 逻辑检查全部通过，只有真机上按住方向键发现毫无反应才会暴露。
/// 这也解释了为什么"方向键控不了进度"和"调不出进度条"是同一个 bug
/// 的两个症状 —— 按键根本就没进来。
///
/// ## v4 的解法：常驻焦点锚点
///
/// 承载视频的全屏 view 自己就是焦点锚点（`canBecomeFocused`）：
///
/// - **看片态 / 进度条态**：它可聚焦并持有焦点，方向键必然被
///   `pressesBegan` 收到，再转给 SwiftUI 的 `handleKey`；
/// - **按钮态**：它关掉 `canBecomeFocused`，焦点交给按钮，左右键由
///   焦点引擎在按钮间移动。
///
/// 两种状态**互斥**，不存在"谁先抢到按键"的仲裁，也就没有 v1/v2 那种
/// 状态残留问题。
///
/// 为什么不用 SwiftUI 的 `.focusable()` 当锚点：tvOS 15 没有
/// `focusEffectDisabled()`（那是 tvOS 17 才有的），`.focusable()` 会给
/// 全屏 view 画一圈白色光晕。UIKit 层的 UIView 默认 `focusEffect` 为
/// nil，没有视觉副作用。
struct PlayerView: View {
    @EnvironmentObject private var engine: PlaybackEngine

    // MARK: - 焦点（只用于按钮之间）

    /// 控制层按钮。`nil` == 焦点不在任何按钮上。
    enum Control: Hashable {
        case rewind, previous, playPause, next, forward, mute, info, close
    }

    @FocusState private var focus: Control?

    // MARK: - 控制层层级

    /// 三级累进。`rawValue` 递增，便于写"至少显示到某一级"的判断。
    enum Level: Int, Comparable {
        case hidden = 0      // 只有视频
        case progress = 1    // 进度条 + 时间
        case buttons = 2     // 进度条 + 按钮行

        static func < (a: Level, b: Level) -> Bool { a.rawValue < b.rawValue }
    }

    @State private var level: Level = .progress

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

            // 点按视频区域 = 暂停并呼出进度条。
            //
            // 关键：只在看片态启用。tvOS 的 UITapGestureRecognizer 设了
            // allowedPressTypes=[.select] 后会**无视焦点**接收按键 ——
            // 若按钮态还开着，按确定键会同时触发"按钮动作"和这个手势，
            // 表现为点一下暂停又立刻恢复（看起来像没反应）。
            //
            // capturesKeys 与 tapEnabled 是两件事，不能合并：
            //   - 看片态：捕获按键 ✓，点按 ✓
            //   - 进度条态：捕获按键 ✓（左右要 seek），点按 ✗（已在播控界面里）
            //   - 按钮态：捕获按键 ✗（让给焦点引擎），点按 ✗
            VideoLayerView(player: engine.avPlayer,
                           tapEnabled: level == .hidden,
                           capturesKeys: level != .buttons && !showInspector,
                           onKey: handleKey,
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

            if level >= .progress {
                progressLayer
                    .transition(.opacity)
            }

            if level >= .buttons {
                buttonsLayer
                    .transition(.opacity)
            }

            if showInspector {
                inspectorOverlay
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: level)
        .animation(.easeInOut(duration: 0.18), value: showInspector)
        .animation(.easeInOut(duration: 0.15), value: seekHint)
        // 播放键与菜单键的 SwiftUI 路径：**只在按钮态**有效。
        // 其余两态焦点在 UIKit 视频锚点上，由 handleKey 处理，
        // 这里收不到，所以不存在双触发。
        .onPlayPauseCommand {
            guard level == .buttons else { return }
            engine.togglePlayPause()
            restartHide()
        }
        .onExitCommand(perform: handleExitFromButtons)
        // 按钮态的「下」= 收起控制层。
        // 其余两态方向键已被 UIKit 层吃掉，这条 guard 保证不会重复处理。
        .onMoveCommand { direction in
            guard level == .buttons else { return }
            if direction == .down { withAnimation { level = .hidden } }
        }
        .onChange(of: level) { newLevel in
            if newLevel == .buttons {
                focusToPlayPause()
                hideTask?.cancel()
                hideTask = nil
            } else {
                // 离开按钮态：释放按钮焦点，焦点自然回到 UIKit 视频锚点
                if focus != nil { focus = nil }
                if newLevel == .progress {
                    restartHide()
                } else {
                    hideTask?.cancel()
                    hideTask = nil
                }
            }
        }
        // 保险：按钮态里焦点若意外变 nil（焦点引擎找不到下一个目标），
        // 整个视图树就没有可聚焦元素了 —— 方向键会再次失灵。
        // 这时退回进度条态，让视频锚点重新接住按键。
        .onChange(of: focus) { newFocus in
            if level == .buttons && newFocus == nil {
                withAnimation { level = .progress }
            }
        }
        .onAppear { restartHide() }
        .onDisappear {
            hideTask?.cancel()
            seekHintTask?.cancel()
        }
    }

    // MARK: - 按键（UIKit 层转来的统一入口）

    /// 方向键 / 播放键 / 菜单键。
    ///
    /// 三个键都从 `PlayerContainerView.pressesBegan` 转进来，
    /// 而不是 SwiftUI 的 `onMoveCommand` —— 原因见文件头的 v3 说明。
    private func handleKey(_ key: KeyEvent) {
        // 检查器打开时把按键让给它自己的焦点处理，只保留"关掉检查器"
        if showInspector {
            if case .menu = key {
                withAnimation { showInspector = false }
            }
            return
        }

        switch key {
        case .move(let direction):
            handleMove(direction)
        case .playPause:
            engine.togglePlayPause()
            restartHide()
        case .menu:
            handleMenu()
        }
    }

    private func handleMove(_ direction: MoveCommandDirection) {
        switch direction {
        case .left:
            seek(-15)
        case .right:
            seek(15)
        case .up:
            // **上下键都能呼出进度条**（用户明确要求）。
            // 已经在控制层里时，上 = 收起。
            switch level {
            case .hidden:
                withAnimation { level = .progress }
            case .progress, .buttons:
                withAnimation { level = .hidden }
            }
        case .down:
            // 逐级展开：看片态 → 进度条 → 按钮行 → 收起
            switch level {
            case .hidden:
                withAnimation { level = .progress }
            case .progress:
                withAnimation { level = .buttons }
            case .buttons:
                // 正常到不了：按钮态把按键让给了焦点引擎，
                // 这里只在焦点意外丢失时兜底。
                withAnimation { level = .hidden }
            }
        @unknown default:
            break
        }
    }

    /// 按钮态下按返回键（走 SwiftUI 的 onExitCommand）。
    private func handleExitFromButtons() {
        // 非按钮态时焦点在 UIKit 锚点上，SwiftUI 根本收不到这个命令；
        // 万一收到也**绝不能**在这里 stop()，否则会和 UIKit 层的
        // menu 处理叠加成"按一次返回直接退出播放"。
        guard level == .buttons else { return }
        if showInspector {
            withAnimation { showInspector = false }
            return
        }
        withAnimation { level = .hidden }
    }

    /// 看片态 / 进度条态下按菜单键（走 UIKit 层）。
    ///
    /// 看片态直接退出播放；有控制层时先收起 —— 和原来的两步退出一致。
    private func handleMenu() {
        if showInspector {
            withAnimation { showInspector = false }
            return
        }
        if level == .hidden {
            engine.stop()
        } else {
            withAnimation { level = .hidden }
        }
    }

    private func seek(_ delta: Double) {
        engine.seek(by: delta)
        showSeekHint(Int(delta))
        // 快进快退必须能看见进度条，否则用户不知道跳到哪了
        if level == .hidden {
            withAnimation { level = .progress }
        }
        restartHide()
    }

    // MARK: - 控制层（一）：进度条

    /// 进度条层。只有进度条和时间，没有按钮。
    ///
    /// 单独成层是这次改动的核心：用户按一下方向键只想要"看看进度、
    /// 拖一拖"，不该被迫先看到一排按钮。
    private var progressLayer: some View {
        VStack {
            Spacer()
            progressBar
                .padding(.horizontal, 70)
                .padding(.bottom, 50)
                .background(alignment: .bottom) {
                    LinearGradient(colors: [.clear, .black.opacity(0.85)],
                                   startPoint: .top, endPoint: .bottom)
                        .frame(height: 200)
                        .ignoresSafeArea(edges: .bottom)
                }
        }
    }

    // MARK: - 控制层（二）：按钮行

    /// 按钮行层。进度条仍在（在按钮行上方），因为拖动进度时
    /// 需要同时看到时间。
    private var buttonsLayer: some View {
        VStack {
            Spacer()
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
    }

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
                engine.seek(by: -15); showSeekHint(-15); restartHide()
            }

            CircleButton(icon: "backward.end.fill", size: 26,
                         focus: $focus, tag: .previous) {
                engine.previous(); restartHide()
            }

            CircleButton(icon: engine.state == .playing ? "pause.fill" : "play.fill",
                         size: 40, prominent: true,
                         focus: $focus, tag: .playPause) {
                engine.togglePlayPause(); restartHide()
            }

            CircleButton(icon: "forward.end.fill", size: 26,
                         focus: $focus, tag: .next) {
                engine.next(); restartHide()
            }

            CircleButton(icon: "goforward.15", size: 26,
                         focus: $focus, tag: .forward) {
                engine.seek(by: 15); showSeekHint(15); restartHide()
            }

            Spacer()

            CircleButton(icon: engine.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill",
                         size: 22, focus: $focus, tag: .mute) {
                engine.setMuted(!engine.isMuted); restartHide()
            }

            CircleButton(icon: "info.circle", size: 22,
                         focus: $focus, tag: .info) {
                withAnimation { showInspector.toggle() }; restartHide()
            }

            CircleButton(icon: "xmark", size: 22,
                         focus: $focus, tag: .close) {
                engine.stop()
            }
        }
        .focusSection()
    }

    // MARK: - 顶部信息条（随按钮态一起出现）

    private var topBar: some View {
        HStack {
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
    }

    private var modeLabel: String {
        switch engine.playMode {
        case .normal: return "顺序播放"
        case .repeatAll: return "列表循环"
        case .repeatOne: return "单集循环"
        case .shuffle: return "随机播放"
        }
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

    /// 点按视频区域：暂停并呼出进度条
    private func pauseAndReveal() {
        if engine.state == .playing {
            engine.togglePlayPause()
        }
        withAnimation { level = .progress }
        restartHide()
    }

    private func focusToPlayPause() {
        // 等按钮真正上屏后再落焦点，否则按钮还不存在
        DispatchQueue.main.async {
            DispatchQueue.main.async {
                guard level == .buttons, !showInspector else { return }
                focus = .playPause
            }
        }
    }

    /// 重新开始自动隐藏计时。
    ///
    /// 三条保护：
    /// 1. 暂停 / 缓冲时不隐藏（此时用户大概率想操作）
    /// 2. 检查器打开时不隐藏
    /// 3. 按钮态且焦点在按钮上时不隐藏（用户正在选按钮）
    private func restartHide() {
        hideTask?.cancel()
        hideTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard !Task.isCancelled else { return }
            guard engine.state == .playing else { return }
            guard !showInspector else { return }
            if level == .buttons && focus != nil { return }
            withAnimation { level = .hidden }
        }
    }
}

// MARK: - 按键事件

/// UIKit 层捕获到的按键。
///
/// 用自定义枚举而不是直接转发 `UIPress`：UIKit 的 `UIPress` 是
/// 瞬态对象，出了 `pressesBegan` 作用域就不可靠；而且 SwiftUI 侧
/// 也不该接触 UIKit 类型。
enum KeyEvent {
    case move(MoveCommandDirection)
    case playPause
    case menu
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

// MARK: - AVPlayerLayer 桥接 + 焦点锚点

/// SwiftUI 在 tvOS 上没有原生 AVPlayerLayer 视图，必须用 UIViewRepresentable 桥接。
///
/// 这个 view 同时承担两件事：
///
/// 1. **承载画面**（`layerClass = AVPlayerLayer`）
/// 2. **焦点锚点 + 按键入口**（`canBecomeFocused` / `pressesBegan`）
///
/// 第 2 点是 v4 修 bug 的关键。tvOS 的方向键由焦点引擎派发，
/// **没有焦点元素 = 收不到方向键**。把画面层做成常驻焦点锚点后，
/// 看片态和进度条态下方向键一定有落点，`onMoveCommand` 那种
/// "代码写了但永远不执行"的死分支问题就不存在了。
///
/// 点按识别也放在 UIKit 这层：tvOS 15 的 SwiftUI 没有 `onTapGesture`
/// 的点击语义，而给 SwiftUI 视图加 `.focusable(true)` 又会和焦点引擎抢焦点。
struct VideoLayerView: UIViewRepresentable {
    let player: AVPlayer
    /// 仅在控制层隐藏（看片态）时为 true。控制层显示时必须为 false，
    /// 否则确定键会绕过焦点引擎被手势抢走，按钮点一下等于点两下。
    var tapEnabled: Bool = false
    /// 是否由本层接管方向键 / 播放键 / 菜单键。
    /// 按钮态必须为 false，把方向键让给焦点引擎做按钮间移动。
    var capturesKeys: Bool = true
    var onKey: ((KeyEvent) -> Void)?
    var onTap: (() -> Void)?

    func makeUIView(context: Context) -> PlayerContainerView {
        let view = PlayerContainerView()
        view.backgroundColor = .black
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspect
        view.isUserInteractionEnabled = true
        view.capturesKeys = capturesKeys
        view.onKey = onKey

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

        let wasCapturing = uiView.capturesKeys
        uiView.capturesKeys = capturesKeys
        uiView.onKey = onKey

        // 从按钮态回到看片/进度条态：让焦点重新落回画面层。
        // 不主动要一次焦点更新的话，tvOS 可能还停在"没有焦点"的状态上，
        // 表现为控制层收起了但方向键依然没反应 —— 也就是这次要修的病。
        if capturesKeys && !wasCapturing {
            uiView.setNeedsFocusUpdate()
            uiView.updateFocusIfNeeded()
        }
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

/// 承载 AVPlayerLayer 的全屏 view，同时是焦点锚点。
final class PlayerContainerView: UIView {
    override static var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }

    /// true = 本层接管按键并持有焦点；false = 让位给 SwiftUI 按钮。
    var capturesKeys = true
    var onKey: ((KeyEvent) -> Void)?

    /// 焦点锚点的开关。
    ///
    /// 动态返回而不是常量 true：按钮态必须让位，否则全屏画面层会
    /// 一直霸着焦点，按钮永远聚焦不上，左右键也就永远在 seek。
    override var canBecomeFocused: Bool { capturesKeys }

    /// 不要焦点外观。
    ///
    /// tvOS 给可聚焦 view 默认画一圈白色光晕 + 轻微放大，在整屏视频上
    /// 会非常突兀。UIView 默认 `focusEffect` 就是 nil，这里显式写出来
    /// 是为了防止哪天有人给父类加了效果而这里跟着继承。
    override var focusEffect: UIFocusEffect? { nil }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        // 首次上屏主动要一次焦点：否则要等用户先按一次键
        // 焦点才会被分配，那第一下按键就丢了。
        if window != nil && capturesKeys {
            setNeedsFocusUpdate()
            updateFocusIfNeeded()
        }
    }

    override func pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        guard capturesKeys else {
            // 按钮态：一律放行，让 SwiftUI 与焦点引擎按自己的逻辑处理
            super.pressesBegan(presses, with: event)
            return
        }

        for press in presses {
            guard let type = press.type else { continue }
            switch type {
            case .upArrow:
                onKey?(.move(.up)); return
            case .downArrow:
                onKey?(.move(.down)); return
            case .leftArrow:
                onKey?(.move(.left)); return
            case .rightArrow:
                onKey?(.move(.right)); return
            case .playPause:
                onKey?(.playPause); return
            case .menu:
                onKey?(.menu); return
            default:
                continue
            }
        }
        // select 与其它按键交给手势识别器 / 系统
        super.pressesBegan(presses, with: event)
    }
}