import SwiftUI

/// 设置页。对齐 UnPlay 设置项，但只保留真正有实现的部分——
/// 不摆空开关，界面上有的都能用。
struct SettingsView: View {
    @EnvironmentObject private var engine: PlaybackEngine
    @EnvironmentObject private var dlna: DLNAService
    @Environment(\.dismiss) private var dismiss

    @AppStorage("uncast.device.name") private var deviceName = "UnCast (Apple TV)"
    @AppStorage("uncast.playmode") private var savedPlayMode = PlaybackEngine.PlayMode.repeatAll.rawValue

    @State private var showAddressAlert = false

    var body: some View {
        NavigationView {
            List {
                // MARK: 设备信息
                Section {
                    settingRow("设备名称", deviceName)
                    settingRow("IP 地址", dlna.localIP)
                    settingRow("服务端口", "9030")
                    settingRow("设备 UUID", String(dlna.uuid.prefix(8)) + "…")
                    settingRow("运行状态", dlna.isRunning ? "运行中" : "已停止")
                } header: {
                    Text("设备信息")
                } footer: {
                    Text("手机投屏时在设备列表里显示的名字就是「设备名称」。修改后需重启服务生效。")
                }

                // MARK: 播放
                Section {
                    Picker("默认播放模式", selection: $savedPlayMode) {
                        Text("顺序播放").tag(PlaybackEngine.PlayMode.normal.rawValue)
                        Text("列表循环").tag(PlaybackEngine.PlayMode.repeatAll.rawValue)
                        Text("单集循环").tag(PlaybackEngine.PlayMode.repeatOne.rawValue)
                        Text("随机播放").tag(PlaybackEngine.PlayMode.shuffle.rawValue)
                    }
                    .onChange(of: savedPlayMode) { newValue in
                        if let m = PlaybackEngine.PlayMode(rawValue: newValue) {
                            engine.playMode = m
                        }
                    }

                    HStack {
                        Text("音量")
                        Spacer()
                        Text("\(engine.volumePercent)%")
                            .foregroundStyle(.secondary)
                    }

                    HStack(spacing: 20) {
                        Button {
                            engine.setVolumePercent(max(0, engine.volumePercent - 10))
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        Button {
                            engine.setVolumePercent(min(100, engine.volumePercent + 10))
                        } label: {
                            Image(systemName: "plus.circle")
                        }
                        Button {
                            engine.setMuted(!engine.isMuted)
                        } label: {
                            Image(systemName: engine.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                        }
                    }
                } header: {
                    Text("播放")
                }

                // MARK: 服务
                Section {
                    Button {
                        if dlna.isRunning { dlna.stop() } else { dlna.start() }
                    } label: {
                        HStack {
                            Text(dlna.isRunning ? "停止投屏服务" : "启动投屏服务")
                            Spacer()
                            Image(systemName: dlna.isRunning ? "stop.circle" : "play.circle")
                        }
                    }

                    Button {
                        dlna.restart()
                    } label: {
                        HStack {
                            Text("重启服务")
                            Spacer()
                            Image(systemName: "arrow.clockwise.circle")
                        }
                    }

                    Button {
                        // tvOS 没有 UIPasteboard，这里只显示地址供用户手抄/扫码
                        showAddressAlert = true
                    } label: {
                        HStack {
                            Text("显示投屏网页地址")
                            Spacer()
                            Image(systemName: "link")
                        }
                    }
                } header: {
                    Text("服务控制")
                } footer: {
                    Text("在手机或电脑浏览器打开 http://\(dlna.localIP):9030/ 可直接推送视频到电视。")
                }

                // MARK: 关于
                Section {
                    settingRow("版本", appVersion)
                    settingRow("原型", "UnPlay 兼容重实现")
                    settingRow("DLNA 规范", "DMR-1.50")
                    settingRow("支持格式", "MP4 / MKV / MOV / MPEG / HLS 等")
                } header: {
                    Text("关于")
                } footer: {
                    Text("本应用为独立实现，通过 UPnP/DLNA 标准协议接收投屏。播放兼容性取决于 tvOS 原生 AVPlayer 支持的编码格式。")
                }
            }
            .navigationTitle("设置")
            .alert("投屏网页地址", isPresented: $showAddressAlert) {
                Button("好", role: .cancel) { }
            } message: {
                Text("在手机或电脑浏览器打开：\nhttp://\(dlna.localIP):9030/")
            }
        }
    }

    private var appVersion: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }

    private func settingRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
        }
    }
}

/// 日志页：投屏排查的核心工具。
/// DLNA 出问题时，一看 SSDP 有没有收到 M-SEARCH、二看 SOAP 动作有没有进来。
struct LogView: View {
    @EnvironmentObject private var dlna: DLNAService
    @Environment(\.dismiss) private var dismiss
    @State private var autoScroll = true

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            VStack(spacing: 0) {
                HStack {
                    Text("投屏日志")
                        .font(.system(size: 32, weight: .semibold))
                        .foregroundStyle(.white)
                    Spacer()
                    Text("\(dlna.logs.count) 条")
                        .font(.system(size: 18))
                        .foregroundStyle(.white.opacity(0.5))
                    Button("关闭") { dismiss() }
                        .padding(.leading, 20)
                }
                .padding(.horizontal, 60)
                .padding(.vertical, 36)

                if dlna.logs.isEmpty {
                    Spacer()
                    VStack(spacing: 14) {
                        Image(systemName: "text.alignleft")
                            .font(.system(size: 46))
                            .foregroundStyle(.white.opacity(0.25))
                        Text("暂无日志")
                            .font(.system(size: 22))
                            .foregroundStyle(.white.opacity(0.45))
                        Text("在手机上发起投屏后，这里会显示 SSDP 与 SOAP 交互过程")
                            .font(.system(size: 17))
                            .foregroundStyle(.white.opacity(0.3))
                    }
                    Spacer()
                } else {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 6) {
                                ForEach(dlna.logs) { entry in
                                    HStack(alignment: .top, spacing: 16) {
                                        Text(Self.timeFormatter.string(from: entry.time))
                                            .font(.system(size: 15, design: .monospaced))
                                            .foregroundStyle(.white.opacity(0.35))
                                            .frame(width: 100, alignment: .leading)
                                        Text(entry.message)
                                            .font(.system(size: 16, design: .monospaced))
                                            .foregroundStyle(colorFor(entry.message))
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    .id(entry.id)
                                }
                            }
                            .padding(.horizontal, 60)
                            .padding(.bottom, 60)
                        }
                        .onChange(of: dlna.logs.count) { _ in
                            if autoScroll, let last = dlna.logs.last {
                                withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                            }
                        }
                    }
                }
            }
        }
    }

    private func colorFor(_ message: String) -> Color {
        if message.contains("❌") || message.contains("失败") || message.contains("⚠️") {
            return Color.orange
        }
        if message.contains("✅") || message.contains("📺") || message.contains("📲") {
            return Color.green.opacity(0.9)
        }
        if message.contains("M-SEARCH") {
            return Color.cyan.opacity(0.85)
        }
        if message.contains("SOAP") {
            return Color.yellow.opacity(0.85)
        }
        return Color.white.opacity(0.72)
    }
}