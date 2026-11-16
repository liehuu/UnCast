import SwiftUI
import AVFoundation

/// App 入口。tvOS 单窗口，不需要 SceneDelegate 的复杂逻辑。
@main
struct UnCastApp: App {

    @StateObject private var engine: PlaybackEngine
    @StateObject private var dlna: DLNAService

    init() {
        let engine = PlaybackEngine()
        _engine = StateObject(wrappedValue: engine)
        _dlna = StateObject(wrappedValue: DLNAService(engine: engine))
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(engine)
                .environmentObject(dlna)
                .onAppear {
                    // 启动即开始接收投屏，符合 UnPlay 的使用习惯（打开就能投）
                    dlna.start()
                }
        }
    }
}

/// 根视图：管理主界面与播放器界面的切换
struct RootView: View {
    @EnvironmentObject private var engine: PlaybackEngine
    @EnvironmentObject private var dlna: DLNAService
    @State private var showSettings = false
    @State private var showLogs = false

    var body: some View {
        ZStack {
            // 有播放内容且未停止 → 全屏播放器
            if engine.currentItem != nil && engine.state != .stopped {
                PlayerView()
                    .transition(.opacity)
            } else {
                HomeView(showSettings: $showSettings, showLogs: $showLogs)
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.25), value: engine.state == .stopped)
        .fullScreenCover(isPresented: $showSettings) {
            SettingsView()
                .environmentObject(engine)
                .environmentObject(dlna)
        }
        .fullScreenCover(isPresented: $showLogs) {
            LogView()
                .environmentObject(dlna)
        }
    }
}