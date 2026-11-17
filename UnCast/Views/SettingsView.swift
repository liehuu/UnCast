import SwiftUI

/// 设置页。对齐 UnPlay 设置项，但只保留真正有实现的部分——
/// 不摆空开关，界面上有的都能用。
struct SettingsView: View {
    @EnvironmentObject private var engine: PlaybackEngine
    @EnvironmentObject private var dlna: DLNAService
    @Environment(\.dismiss) private var dismiss

    @AppStorage("uncast.device.name") private var deviceName = "UnCast (Apple TV)"
    @AppStorage("uncast.playmode") private var savedPlayMode = PlaybackEngine.PlayMode.repeatAll.rawValue
    @AppStorage("uncast.ignoreRemoteStop") private var ignoreRemoteStop = true

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

                // MARK: 投屏兼容性
                //
                // 这一段是为了让用户能自己分清两种「搜不到」：
                // 1. 对方 App 只走 AirPlay/私有协议 —— 结构上不会看见 DLNA 设备，改不了；
                // 2. 对方 App 走 DLNA 但没发现我们 —— 看下面 M-SEARCH 计数即可判定。
                // 没有这段，用户只能靠猜，而猜测的默认结论往往是「这个 App 坏了」。
                Section {
                    settingRow("SSDP 搜索请求", "\(dlna.mSearchCount) 次")
                    settingRow("已回复设备", "\(dlna.replyCount) 条")
                    settingRow("通告次数", "\(dlna.notifyCount) 次")
                    settingRow("支持的标准", "DLNA / UPnP · 仅此一种")
                } header: {
                    Text("投屏兼容性")
                } footer: {
                    Text("UnCast 只实现 DLNA/UPnP 接收端（DMR-1.50）。若某个 App 的投屏列表里始终没有这台电视，先让它的投屏列表保持打开，再看「SSDP 搜索请求」是否增加：\n• 增加 = 它在找 DLNA 设备，是兼容性问题，可查日志定位；\n• 一直为 0 = 它只走 AirPlay 或自家私有协议（如「电视联播码」），这类 App 无法发现任何第三方 DLNA 设备——包括原版 UnPlay。\nAirPlay 投屏由 tvOS 系统接收，不经本应用；那条路径上手机锁屏会中断播放。")
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

                    Toggle(isOn: $ignoreRemoteStop) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("手机退出后继续播放")
                            Text("忽略手机端发来的停止指令，适合手机走开、电视继续放的场景")
                                .font(.system(size: 14))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .onChange(of: ignoreRemoteStop) { newValue in
                        engine.ignoreRemoteStop = newValue
                    }
                } header: {
                    Text("播放")
                } footer: {
                    Text("DLNA 投屏时电视直接从源站拉流，手机只负责下指令。因此锁屏、切后台、退出 App 都不影响播放——除非客户端主动发停止指令。打开上面开关可忽略这类指令。")
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
            .onAppear {
                // 引擎已在 init 里从 UserDefaults 恢复过设置，
                // 这里只做一次双向校正：界面显示值以引擎当前值为准，
                // 避免出现「开关显示开、引擎实际是关」的不一致。
                ignoreRemoteStop = engine.ignoreRemoteStop
                savedPlayMode = engine.playMode.rawValue
            }
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