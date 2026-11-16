import SwiftUI

/// 主界面（未播放时显示）。
///
/// 对标 UnPlay 的首页：
/// - 显示设备名与 IP，方便用户确认投屏目标
/// - 显示运行状态与已接收投屏次数
/// - 展示接收到的播放列表
/// - 提供设置 / 日志入口
struct HomeView: View {
    @EnvironmentObject private var dlna: DLNAService
    @EnvironmentObject private var engine: PlaybackEngine
    @Binding var showSettings: Bool
    @Binding var showLogs: Bool

    /// 用透明度做呼吸动画（tvOS 15 不支持 symbolEffect，需 tvOS 17+）
    @State private var pulseOpacity: Double = 1.0

    var body: some View {
        ZStack {
            // 背景渐变，tvOS 上深色更舒服
            LinearGradient(
                colors: [Color(red: 0.06, green: 0.07, blue: 0.12),
                         Color(red: 0.12, green: 0.10, blue: 0.20)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 48) {
                    header
                    statusCards
                    if !engine.playlist.isEmpty {
                        playlistSection
                    }
                    actionButtons
                    Spacer(minLength: 40)
                }
                .padding(80)
            }
        }
        .onAppear {
            pulseOpacity = 0.35
        }
    }

    // MARK: - 头部

    private var header: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 16) {
                Image(systemName: dlna.isRunning ? "dot.radiowaves.left.and.right" : "wifi.slash")
                    .font(.system(size: 44, weight: .light))
                    .foregroundStyle(dlna.isRunning ? Color.green : Color.orange)
                    .opacity(dlna.isRunning ? pulseOpacity : 1.0)
                    .animation(
                        dlna.isRunning
                            ? .easeInOut(duration: 1.2).repeatForever(autoreverses: true)
                            : .default,
                        value: pulseOpacity
                    )

                Text("UnCast")
                    .font(.system(size: 64, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
            }

            Text(dlna.statusMessage)
                .font(.system(size: 26, weight: .medium))
                .foregroundStyle(dlna.isRunning ? Color.green.opacity(0.9) : Color.orange)

            HStack(spacing: 28) {
                Label(dlna.friendlyName, systemImage: "appletv")
                Label(dlna.localIP, systemImage: "network")
                Label(":9030", systemImage: "number")
            }
            .font(.system(size: 20))
            .foregroundStyle(.white.opacity(0.65))
        }
    }

    // MARK: - 状态卡片

    private var statusCards: some View {
        HStack(spacing: 28) {
            InfoCard(
                icon: "arrow.down.circle",
                title: "已接收投屏",
                value: "\(dlna.castCount)",
                unit: "次"
            )
            InfoCard(
                icon: "list.bullet",
                title: "播放列表",
                value: "\(engine.playlist.count)",
                unit: "个"
            )
            InfoCard(
                icon: engine.isMuted ? "speaker.slash" : "speaker.wave.2",
                title: "音量",
                value: "\(engine.volumePercent)",
                unit: "%"
            )
        }
    }

    // MARK: - 播放列表

    private var playlistSection: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("播放列表")
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))

            VStack(spacing: 0) {
                ForEach(Array(engine.playlist.enumerated()), id: \.element.id) { index, item in
                    HStack(spacing: 18) {
                        Text("\(index + 1)")
                            .font(.system(size: 20, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.4))
                            .frame(width: 44, alignment: .trailing)

                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.title)
                                .font(.system(size: 22, weight: .medium))
                                .foregroundStyle(.white)
                                .lineLimit(1)
                            Text(item.url.host ?? item.url.absoluteString)
                                .font(.system(size: 16))
                                .foregroundStyle(.white.opacity(0.4))
                                .lineLimit(1)
                        }

                        Spacer()

                        if index == engine.currentIndex {
                            Text("当前")
                                .font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(Color.accentColor)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 5)
                                .background(Color.accentColor.opacity(0.18), in: Capsule())
                        }
                    }
                    .padding(.vertical, 14)
                    .padding(.horizontal, 20)

                    if index < engine.playlist.count - 1 {
                        Divider().background(Color.white.opacity(0.08))
                    }
                }
            }
            .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 16))
        }
    }

    // MARK: - 按钮

    private var actionButtons: some View {
        HStack(spacing: 24) {
            ActionButton(icon: "gearshape", title: "设置") { showSettings = true }
            ActionButton(icon: "doc.text.magnifyingglass", title: "投屏日志") { showLogs = true }
            ActionButton(icon: "arrow.clockwise", title: "重启服务") { dlna.restart() }
            if !engine.playlist.isEmpty {
                ActionButton(icon: "play.fill", title: "继续播放") {
                    if engine.state == .stopped { engine.play() }
                }
            }
        }
    }
}

// MARK: - 复用组件

struct InfoCard: View {
    let icon: String
    let title: String
    let value: String
    let unit: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(Color.accentColor)

            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value)
                    .font(.system(size: 44, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                Text(unit)
                    .font(.system(size: 18))
                    .foregroundStyle(.white.opacity(0.45))
            }

            Text(title)
                .font(.system(size: 17))
                .foregroundStyle(.white.opacity(0.55))
        }
        .frame(minWidth: 240, alignment: .leading)
        .padding(28)
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 18))
    }
}

/// 可聚焦的按钮（tvOS 焦点引擎在 focusable + onTapGesture 下工作良好）
struct ActionButton: View {
    let icon: String
    let title: String
    let action: () -> Void
    @FocusState private var focused: Bool

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.system(size: 22, weight: .medium))
                Text(title)
                    .font(.system(size: 22, weight: .medium))
            }
            .padding(.horizontal, 32)
            .padding(.vertical, 18)
            .background(
                RoundedRectangle(cornerRadius: 14)
                    .fill(focused ? Color.white : Color.white.opacity(0.10))
            )
            .foregroundStyle(focused ? Color.black : Color.white)
            .scaleEffect(focused ? 1.06 : 1.0)
            .animation(.easeOut(duration: 0.15), value: focused)
        }
        .buttonStyle(.plain)
        .focused($focused)
    }
}