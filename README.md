# UnCast — Apple TV 上的 DLNA 投屏接收器

UnPlay 的开源替代实现。用 UPnP/DLNA 标准协议接收手机投屏，tvOS 原生播放。

> **为什么要重写**：UnPlay 是闭源付费 App（¥15），源码未公开，仓库只有文档。
> 本项目的目标不是复制它的代码，而是用公开的 DLNA 协议标准做一个功能对等的接收器。

---

## 功能对照

| UnPlay 功能 | UnCast 实现 | 说明 |
|---|---|---|
| DLNA 投屏接收 | ✅ | SSDP 发现 + AVTransport/RenderingControl/ConnectionManager 三服务 |
| 播放器 | ✅ | 原生 AVPlayer，支持 HDR / Dolby Vision（取决于片源与 tvOS 设置） |
| 播放列表 | ✅ | 列表循环 / 单集循环 / 顺序 / 随机 |
| 进度控制 | ✅ | 遥控器左右滑动 ±15 秒，进度条显示 |
| 音量控制 | ✅ | RenderingControl SetVolume，界面同步 |
| 检查器 | ✅ | 上滑呼出，显示状态/时长/分辨率/协议 |
| HTTP 投屏 API | ✅ | 完全兼容 UnPlay 的 `POST /` 与 `GET /PlaybackEvent` |
| 网页投屏页 | ✅ | 浏览器打开 `http://电视IP:9030/` |
| 内置网页二维码 | ⚠️ | 只显示地址，未生成二维码 |
| FFmpeg 播放内核 | ❌ | 只用 AVPlayer；不支持的编码需转码 |
| Top Shelf 小组件 | ❌ | 未实现（需要 TVServices Extension） |
| 时钟 / 天气 / 壁纸场景 | ❌ | 未实现（属氛围屏功能，与投屏核心无关） |
| 图标主题切换 | ❌ | 未实现 |

**核心投屏能力完整可用**，氛围屏与 Top Shelf 属附加功能，按需再加。

---

## 构建与安装

### 已构建好的包

仓库 `dist/UnCast-1.0.0.tipa` 就是可直接安装的成品（由 GitHub Actions 构建）。

| 项 | 值 |
|---|---|
| 大小 | 1,753,217 字节（1.67 MB） |
| sha256 | `8068c3d4935e9af02d2bbc4a70521861e5af3e6b02237ed0843b79de50f725a5` |
| 架构 | arm64 |
| 平台 | tvOS（`LC_BUILD_VERSION.platform = 3`，minos 15.0） |
| 加密 | 未加密（`cryptid = 0`） |
| 签名 | ad-hoc（含 `LC_CODE_SIGNATURE`，dataSize > 0） |
| 多播权限 | `com.apple.developer.networking.multicast = true` |
| 主屏图标 | 内置（`Assets.car` 1.46 MB，含 `App Icon` 三层 + `Top Shelf Image`） |
| 构建 | GitHub Actions run #34（`d9432c8`），17 步全绿 |

下载：<https://github.com/liehuu/UnCast/releases/download/v1.0.0/UnCast-1.0.0.tipa>

上述指标是读 tipa 内二进制的原始字节实测得出，可用
`python3 scripts/check_tipa.py <tipa>` 自行复现（16 项断言）。
sha256 已通过「匿名下载回来再比对」验证，不是只信上传返回码。

> **图标不需要你自己提供。** `scripts/make_icon.py` 用纯 Python（只依赖 numpy）
> 按 tvOS 规范生成全部 13 张图：主屏图标三层的 @1x/@2x（400×240 / 800×480）、
> App Store 图标 1280×768、Top Shelf 常规与宽屏的 @1x/@2x。
> 想换配色或图形，改这个脚本里的几何参数重跑即可，不需要美术资源。

### TrollStore 安装步骤

1. 把 `UnCast-1.0.0.tipa` 传到 iPhone / iPad，或用「文件」App 存到 iCloud Drive
2. 在 Apple TV 上打开 TrollStore → **Install from File**（或通过 Filza / 局域网上传）
3. 选中 `UnCast-1.0.0.tipa` → 安装
4. 首次启动会询问「本地网络」权限，**必须允许**，否则收不到投屏

> 为什么不用 App Store 的 ipa：App Store 下发的 tvOS 二进制带 FairPlay 加密
> （`cryptid != 0`），装到非授权设备上会直接崩。必须自构建未加密包。

### 方式一：GitHub Actions 出 tipa

1. 把本目录推到 GitHub 仓库
2. Actions 会自动跑 `Build UnCast tipa`
3. 下载 artifact `UnCast-tipa`
4. 传到 Apple TV，用 TrollStore 打开安装

CI 里有两道关卡，任意一道不通过都会变红并阻止上传：

- `Ad-hoc sign with entitlements` — 用 `ldid -S` 把多播权限写进二进制
- `Gate — verify artifact is installable on tvOS` — 跑 `scripts/check_tipa.py`，
  直接读原始字节校验平台 / 加密状态 / 权限

### 方式二：本地 Mac

```bash
brew install xcodegen ldid
xcodegen generate
xcodebuild -project UnCast.xcodeproj -scheme UnCast -sdk appletvos \
  -configuration Release CODE_SIGNING_ALLOWED=NO build
# 之后手动 ldid -SUnCast/App/UnCast.entitlements <产物>/UnCast.app/UnCast
# 再用 scripts/check_tipa.py 验证
```

---

## 手机离开后继续播放（重点）

用户最关心的一条：**手机锁屏、切后台、退出投屏 App，电视上的播放不能中断。**

这条在 DLNA 架构下**天然成立**，原因是：

```
手机 App                    源站                      Apple TV
   │                          │                          │
   │ ① 把 URL 发给电视 ─────────────────────────────────▶ │
   │    (SetAVTransportURI)   │                          │
   │                          │ ② 电视自己拉流 ─────────▶ │
   │                          │◀──────────────────────── │
   │ ③ 只发控制指令（Play/Pause/Seek）                    │
   │    手机在这里关机也不会影响 ② 的已经建立的连接         │
```

电视是**自己**从源站拉流的，手机只负责下发指令。所以手机退出不会掐断数据流。

真正会中断播放的只有两类情况，本项目都做了处理：

| 风险 | 处理方式 | 代码位置 |
|---|---|---|
| 客户端退出时主动发 `AVTransport Stop` | `ignoreRemoteStop` 开关（默认开），远端 Stop 被忽略并计数 | `PlaybackEngine.stop(isRemote:)` |
| 音频被系统打断（来电、其他 App 抢占） | 监听 `AVAudioSession.interruptionNotification`，结束后自动续播 | `PlaybackEngine.setupLifecycleHandling()` |
| 网络瞬时抖动 | 前向缓冲 30 秒 + 允许暂停时继续缓冲 + 自动等待缓冲 + 瞬时错误重试一次 | `loadAndPlay` / `scheduleRetryIfTransient` |
| App 被系统挂起 | `UIBackgroundModes = ["audio"]`，后台保持出声 | `Info.plist` |

设置页有一个开关「手机退出后继续播放」，默认开启。关掉后行为退回标准 DLNA
（远端 Stop 立即生效），用于那些确实需要「手机停、电视也停」的场景。

播放界面右上角会显示 **「已保护 N 次」**，即拦下了多少次远端 Stop，
可以用来确认这个机制真的在工作。

### 验证方式

`scripts/verify_cast.py` 对协议行为做了端到端断言，其中用例 1 就是这条诉求，
并配了反向用例防止「断言恒真」：

```bash
python3 scripts/verify_cast.py      # 全部通过
python3 scripts/verify_injection.py # 反向注入，证明断言能失败
```

反向注入的结果：故意把 `stop()` 改成「收到 Stop 就停」后，
有 6 条断言变红；改回后复跑全绿。这说明这些断言不是装饰。

---

## 遥控器交互（三个已修缺陷）

这三个是实测报上来的，都属于「看得见但编译期发现不了」：

### 1. 方向键冲突：既想快进又想选按钮

**症状**：播放中按左右，有时快进 15 秒，有时在按钮间移动，取决于当时焦点在哪，
用户无法预期。

**根因**：把「快进」和「按钮间移动」当成两个并行的手势去抢方向键。
只要两者都能响应，就必然有歧义。

**解法**：改成**互斥状态机**。焦点是否在控制层，用 `@FocusState` 的枚举值表示，
`isFocusInControls` 是纯派生：

```swift
enum Control: Hashable {
    case rewind, previous, playPause, next, forward, mute, info, close
}

@FocusState private var focus: Control?

// 纯派生，不存状态 —— 因此不可能残留成 true。
private var isFocusInControls: Bool { focus != nil }
```

方向键只在**一个**地方仲裁：

```swift
private func handleMove(_ direction: MoveCommandDirection) {
    if showInspector { return }

    // ── 选键态 ──
    // 左右/上交给焦点引擎做按钮间移动，这里绝不 seek。
    if isFocusInControls {
        if direction == .down { collapseControls() }
        return
    }

    // ── 看片态 ──
    switch direction {
    case .left:  seek(-15)
    case .right: seek(15)
    case .up, .down:
        revealControls(focusPlay: true)
    @unknown default: break
    }
}
```

顺带解决了另一个隐蔽 bug：按钮不再各自持有 `@FocusState`，而是绑定父级的同一个，
所以「焦点在哪」永远只有一个真相来源。

### 2. 控制层自动隐藏后调不出来

**症状**：播放中控制层 5 秒后自动隐藏，此后按任何键都唤不回。

**根因**：两处叠加 ——
(a) 看片态没有「唤回」的处理分支，方向键什么都不做；
(b) 自动隐藏只看时间，不看焦点，用户正在选按钮时面板也会消失。

**解法**：

```swift
// 看片态下任何方向键都能唤回（左右顺手快进，上下直接呼出）
case .left:  seek(-15)
case .right: seek(15)
case .up, .down:
    revealControls(focusPlay: true)
```

```swift
private func scheduleHide() {
    hideTask?.cancel()
    hideTask = Task { @MainActor in
        try? await Task.sleep(nanoseconds: 5_000_000_000)
        guard !Task.isCancelled else { return }
        // 焦点还在按钮上、或者已经暂停了，就不该隐藏
        guard engine.state == .playing,
              focus == nil,
              !showInspector else { return }
        withAnimation { showControls = false }
    }
}
```

另外补了两条兜底：按播放/暂停键、按返回键都会先把控制层拉回来。

### 3. 没有图标

**症状**：装到 Apple TV 上主屏是白块。

**根因有三层，只修一层不够**：

1. 项目里根本没有 `AppIcon` 资源 → 写了 `scripts/make_icon.py` 生成全部 13 张图
2. `project.yml` 少了 `ASSETCATALOG_COMPILER_APPICON_NAME` → `actool` 根本不产出图标
3. `Info.plist` 的 `CFBundleIcons` 按 iOS 格式写成了字典 → tvOS 读不到

第 3 条最坑：**构建全绿、`Assets.car` 里有图，但主屏还是白块。**
tvOS 与 iOS 的格式不同：

```xml
<!-- tvOS：CFBundlePrimaryIcon 是**字符串** -->
<key>CFBundleIcons</key>
<dict>
    <key>CFBundlePrimaryIcon</key>
    <string>App Icon</string>
</dict>
```

```xml
<!-- iOS：是字典。照抄到 tvOS 上无效 -->
<key>CFBundleIcons</key>
<dict>
    <key>CFBundlePrimaryIcon</key>
    <dict>
        <key>CFBundleIconName</key>
        <string>App Icon</string>
    </dict>
</dict>
```

图标本身是用**纯 Python 手写 PNG 编码器**画的（只依赖 numpy），
带解析式抗锯齿的 SDF 光晕，尺寸与不透明度全部通过断言校验。

---

## 关键技术点

### 开发语言与框架

| 层 | 技术 | 说明 |
|---|---|---|
| 语言 | **Swift 5.9** | 全部业务代码，约 3,470 行 |
| UI | **SwiftUI** | 三个界面：`HomeView` / `PlayerView` / `SettingsView` |
| 播放 | **AVFoundation**（`AVPlayer`） | tvOS 原生硬解，支持 HDR / Dolby Vision |
| 网络 | **Network.framework**（`NWListener` / `NWConnection`） | HTTP 服务端与 SSDP socket，不用第三方库 |
| 组合 | **Combine** | `ObservableObject` 状态广播 |
| 桥接 | **UIKit**（`UIGestureRecognizer` / `UIViewControllerRepresentable`） | 只为拿遥控器确定键，SwiftUI 在 tvOS 15 上做不到 |
| 工程生成 | **XcodeGen**（`project.yml`） | 不把 `.xcodeproj` 提交进仓库，避免合并冲突 |
| 构建 | **xcodebuild** `-sdk appletvos` + **ldid** | ad-hoc 签名并嵌入 entitlements |
| 图标生成 | **Python 3** + **numpy** | 手写 PNG 编码器，零第三方依赖 |
| 校验脚本 | **Python 3**（stdlib） | 门禁不依赖 Xcode，本机可跑 |

部署目标 **tvOS 15.0**，只在 arm64 上构建。没有用任何 CocoaPods / SPM 依赖 ——
整个项目零外部依赖，`xcodegen generate` 之后直接能编译。

之所以钉在 tvOS 15：TrollStore 支持的最低 tvOS 版本就在这一代，
再低的设备装不了巨魔，再高的会丢掉一部分老设备。

**tvOS 15 的 API 限制**（代码里都绕过了）：

- `onTapGesture` 没有点击语义（tvOS 16+），所以确定键走 UIKit 手势
- `symbolEffect` 需要 tvOS 17+，呼吸动画改用 `.opacity` + `.repeatForever`
- 没有 `UIPasteboard`，设置页不提供复制 IP 功能

### 为什么需要 TrollStore（或自签名）

DLNA 的 SSDP 发现依赖 **UDP 组播 239.255.255.250:1900**。tvOS 上要收组播，必须满足：

1. Info.plist 声明 `NSLocalNetworkUsageDescription`
2. entitlement 带 `com.apple.developer.networking.multicast`

第 2 条 Apple 需要单独申请审批，免费个人开发者账号拿不到。**TrollStore 签名可以保留任意 entitlement**，所以这条路反而比 App Store 更合适。

### 投屏链路

```
手机 App                  Apple TV (UnCast)
   │
   ├─ SSDP M-SEARCH ────────▶ :1900  收包，回 200 OK + LOCATION
   │
   ├─ GET /dd.xml ──────────▶ 返回 MediaRenderer 设备描述
   │
   ├─ GET /scpd/AVTransport.xml ▶ 返回动作表
   │
   ├─ POST SetAVTransportURI ──▶ 解析 DIDL-Lite 取标题 + 地址，开始播放
   ├─ POST Play ──────────────▶ player.play()
   └─ POST GetPositionInfo ───▶ 返回当前进度
```

### 端口

| 端口 | 用途 |
|---|---|
| 1900/UDP | SSDP 发现（组播） |
| 9030/TCP | HTTP：设备描述、SOAP 控制、投屏页、API |

9030 与 UnPlay 保持一致，方便复用现有脚本。

---

## 各模块实现说明

### 1. SSDP 发现（`SSDPResponder.swift`，378 行）

设备能被搜到的**唯一**入口。手机发 `M-SEARCH` 到组播地址，TV 必须在 1 秒内回
单播 `200 OK`，带 `LOCATION` 指向自己的设备描述。

```swift
// 组播加入 —— 这行是 SSDP 的核心，失败就彻底搜不到设备
var group = ip_mreq()
group.imr_multiaddr.s_addr = inet_addr("239.255.255.250")
group.imr_interface.s_addr = htonl(INADDR_ANY)
setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &group,
           socklen_t(MemoryLayout<ip_mreq>.size))
```

两个容易踩的点：

- `SO_REUSEADDR` + `SO_REUSEPORT` 都要设。不设的话，Apple TV 自带的
  AirPlay 服务已经占了 1900，`bind` 直接失败。
- 回包要走**单播**（`sendto` 到对方源地址），不要图省事回组播。回组播会
  让同网段所有设备都收到重复响应，客户端可能因此把设备列入去重黑名单。

### 2. HTTP 服务端（`HTTPServer.swift`，305 行）

用 `NWListener` 而不是 `GCDAsyncSocket`，因为它对 tvOS 的本地网络权限适配最好。
手写了一个够用的 HTTP/1.1 解析器（不支持 chunked 请求体 —— DLNA 客户端不用）。

路由表在 `DLNAService.route()` 里，7 条路径：

```
GET  /                      → 投屏网页（HTML）
GET  /dd.xml                → 设备描述
GET  /scpd/AVTransport.xml  → 传输服务动作表
GET  /scpd/RenderingControl.xml → 音量服务动作表
GET  /scpd/ConnectionManager.xml → 连接管理动作表
POST /control/*             → SOAP 控制入口
GET  /PlaybackEvent         → UnPlay 兼容的状态查询
POST /                      → UnPlay 兼容的推送播放
```

### 3. SOAP 处理（`SOAP.swift`，161 行）

DLNA 的控制指令都是 SOAP 信封套 `u:` 命名空间的动作。手写解析而不用
`XMLParser` 的 delegate，因为需要的只是「取某个标签的文本」这一件事。

```swift
// 关键：命名空间前缀不固定，有的客户端用 u:，有的用 s:
// 所以按 localName 匹配，不能按前缀
private func localName(_ raw: String) -> String {
    raw.contains(":") ? String(raw.split(separator: ":").last!) : raw
}
```

`SetAVTransportURI` 里的 `CurrentURIMetaData` 是 DIDL-Lite XML，标题藏在
`<dc:title>` 里。这个字段经常被客户端做 HTML 实体转义，要还原。

### 4. 设备/服务描述（`UPnPDescription.swift`，334 行）

三个 SCPD（服务控制协议描述）是**静态 XML 字符串**。看起来该用文件，
但嵌在代码里有两个好处：不会被 XcodeGen 漏拷进 bundle，也不用管路径。

设备描述里 `deviceType` 必须是 `urn:schemas-upnp-org:device:MediaRenderer:1`，
`friendlyName` 就是手机上显示的名字（默认取 tvOS 设备名）。

### 5. 播放引擎（`PlaybackEngine.swift`，456 行）

`AVPlayer` 的封装。三个设计决定：

**线程契约**：类不标 `@MainActor`（tvOS 15 的并发检查会报一堆警告），
但约定所有公开方法必须在主线程调用，KVO 回调统一派发回主线程再改
`@Published`。`DLNAService` 负责从 HTTP 线程桥过来。

**忽略远端 Stop**（`ignoreRemoteStop`）：默认开启。部分国产 App 在退出播放页
时会发 `AVTransport Stop`，这会让「手机走开、电视继续放」失效。开启后忽略并计数，
界面上显示「已保护 N 次」，让用户能确认机制在工作。

**中断恢复**：监听 `AVAudioSession.interruptionNotification`，被打断时记录
「原本想播」，恢复时自动续播。这是拖地、接电话场景必需的。

### 6. 播放器界面（`PlayerView.swift`，599 行）

三个 bug 的修复都在这，详见上文「遥控器交互」章节。架构上的关键决定：

焦点状态用 `@FocusState<Control?>` 的枚举值作为**唯一真相来源**，
`isFocusInControls` 是纯派生计算属性。这样「忘了清空焦点」在结构上不可能发生 ——
前两版分别用全局拦截和回调 + 布尔标记，都栽在这个状态会残留上。

确定键的处理需要绕一圈：tvOS 15 的 SwiftUI 没有办法「在看片态接收点击」，
所以用 `UIViewControllerRepresentable` 塞了一个 `UITapGestureRecognizer`。
但它有个坑 —— 加了 `allowedPressTypes = [.select]` 之后，**不管焦点在哪都会收到**，
于是按确定键会同时触发按钮动作和手势（暂停又立刻恢复）。
所以它绑了一个 `tapEnabled`，只在控制层隐藏时为 true。

### 7. 首页（`HomeView.swift`，234 行）

未播放时显示：设备名、IP、端口、已接收投屏次数、播放列表、四个操作按钮。
纯展示，没有业务逻辑。用透明度做呼吸动画而不是 `symbolEffect`（tvOS 15 不支持）。

### 8. 设置（`SettingsView.swift`，262 行）

| 设置项 | 存储 | 默认 |
|---|---|---|
| 手机退出后继续播放 | `UserDefaults` `uncast.ignoreRemoteStop` | 开 |
| 播放模式 | `UserDefaults` `uncast.playmode` | 顺序 |
| 设备名 | `UserDefaults` `uncast.friendlyName` | tvOS 设备名 |
| 重启服务 | — | — |

`ignoreRemoteStop` 在 `PlaybackEngine.init()` 里就恢复，不只依赖设置页
`onAppear` 同步 —— 否则用户关掉开关后重启 App，在打开设置页之前引擎仍用默认值，
开关等于失效。

---

## API 兼容性

完全对齐 UnPlay 文档（`suaptv/UnPlay` 的 `httpAPI.md`）：

**查询状态**
```http
GET http://电视IP:9030/PlaybackEvent
```
```json
{
  "total_duration": 936.23,
  "current_time": 5.18,
  "playback_status": "PLAYING",
  "title": "第19集",
  "current_index": 1,
  "total_videos": 3
}
```

**推送播放**
```http
POST http://电视IP:9030/
Content-Type: application/x-www-form-urlencoded

urls=<URL编码的"标题$地址"列表>&option=native&mode=playList
```

`mode`：`none` 单集 / `playList` 列表循环
`option`：接受 `native`，`ffmpeg` 会被忽略（本实现只有原生播放器）

---

## 已知限制

1. **编码兼容性**：AVPlayer 不支持 MKV 里的某些音频编码（如 DTS、部分 AC3 变体），会出现有画面无声音。这是 tvOS 原生限制，UnPlay 靠集成 FFmpeg 解决，本项目未做。
2. **DRM 内容无法播放**：Netflix、Disney+ 等加密流不走 DLNA，也不支持。
3. **HDR 需要 tvOS 侧配合**：设置 → 视频和音频 → 匹配内容 → 打开「匹配动态范围」。
4. **组播受干扰**：YouTube 等 App 可能占用网络端口，投屏搜不到时先关闭其他 App 后台。

---

## 排查

界面里「投屏日志」能看到完整链路：

- 看不到 `M-SEARCH` → SSDP 没收到，检查是否同一局域网、组播是否被拦
- 有 `M-SEARCH` 但没 `SOAP` → 手机拿到了设备描述但没发控制请求，可能 SCPD 解析失败
- 有 `SetAVTransportURI` 但画面黑 → 视频源问题，看错误提示（403 / 超时 / 编码不支持）
- `bind` 失败 → 1900 端口被占用，重启 Apple TV 或关掉其他 DLNA 服务

---

## 目录结构

```
UnCast/
├── project.yml                    # XcodeGen 工程定义
├── .github/workflows/build.yml    # CI：构建 + 三道门禁 + 出 tipa
├── scripts/
│   ├── make_icon.py               # 生成全部 tvOS 图标/Top Shelf（纯 Python）
│   ├── check_tipa.py              # 产物门禁：平台/加密/签名/多播权限
│   ├── check_icon.py              # 图标门禁：源资源 + 包内 Assets.car
│   ├── verify_player.py           # 播放器交互契约（36 项）
│   ├── verify_player_injection.py # 变异测试：证明上面那些断言真会红
│   ├── selftest_icon_paths.py     # check_icon 两条代码路径都跑一遍
│   ├── selftest_tipa.py           # check_tipa 的合成产物回归（不需要 Xcode）
│   └── push_tree.py               # 用 Git Data API 推送（本机 git push 被代理挡）
└── UnCast/
    ├── App/
    │   ├── UnCastApp.swift        # 入口
    │   ├── Info.plist
    │   ├── AppIcon.png            # 参考图（不是兜底机制）
    │   └── UnCast.entitlements    # 组播权限
    ├── Assets.xcassets/
    │   └── App Icon & Top Shelf Image.brandassets/
    ├── DLNA/
    │   ├── SSDPResponder.swift    # 组播发现
    │   ├── HTTPServer.swift       # HTTP 服务端
    │   ├── SOAP.swift             # SOAP 解析
    │   ├── UPnPDescription.swift  # 设备/服务描述
    │   └── DLNAService.swift      # 总协调 + 路由 + API
    ├── Player/
    │   └── PlaybackEngine.swift   # AVPlayer 封装
    └── Views/
        ├── HomeView.swift
        ├── PlayerView.swift
        └── SettingsView.swift
```

---

## 需要你自己准备的资源 / 配置项

先给结论：**跑起来不需要你提供任何素材。** 图标、工程文件、CI 配置全都在仓库里。

| 项 | 是否必填 | 说明 |
|---|---|---|
| Apple 开发者账号 | ❌ 不需要 | 全程 ad-hoc 签名，不碰证书 |
| 图标 / 美术素材 | ❌ 不需要 | `scripts/make_icon.py` 纯 Python 生成 13 张图 |
| Mac + Xcode | ❌ 不需要 | 用 GitHub Actions 的 macOS runner 编译 |
| 付费的 TLS 证书 | ❌ 不需要 | 投屏走局域网明文 HTTP，DLNA 协议本身如此 |
| **局域网同一网段** | ✅ 必须 | 手机和 Apple TV 必须在同一个子网，跨 VLAN 组播过不去 |
| **本地网络权限** | ✅ 必须 | 首次启动弹出时点「允许」，拒绝后 SSDP 收不到任何包 |
| **Apple TV 的 IP** | ⚠️ 需要查 | 用于在浏览器打开投屏页 `http://<IP>:9030/`，在设置页能看到 |
| 转码服务（可选） | ⚠️ 按需 | 只有遇到 DTS / 部分 AC3 音频才需要，见「已知限制」 |
| GitHub 账号 | ⚠️ 换人构建时需要 | 想自己出包就 fork 后 push，Actions 自动跑 |

想改图标配色：编辑 `scripts/make_icon.py` 里的渐变与几何参数，重跑
`python3 scripts/make_icon.py`，再跑 `python3 scripts/check_icon.py .` 确认。

---

## 三道门禁在挡什么

CI 里 `BUILD SUCCEEDED` 只是必要条件，不是充分条件。这个项目踩过的坑里，
有一半是「编译全绿、装上不对」。所以有三道独立的门禁，每道都做过反向注入验证
（故意把缺陷放回去，确认它真的会红）：

| 门禁 | 检查对象 | 断言数 | 挡住的典型缺陷 |
|---|---|---|---|
| Gate 1 | 源码 + 图标源资源 | 36 + 34 + 17 + 12 | 方向键冲突、控制层唤不回、图标尺寸/透明度错、assert 永真 |
| Gate 2 | 编译产物 `Assets.car` | 11 | `ASSETCATALOG_COMPILER_APPICON_NAME` 漏配 → 图标不进包 |
| Gate 3 | tipa 字节 | 16 + 20 | 平台不是 tvOS、被 FairPlay 加密、缺多播权限、空签名区 |

其中两条是本项目真实踩过的、且**构建阶段完全不报错**的：

1. **tvOS 的 `CFBundlePrimaryIcon` 必须是字符串**，不是 iOS 那种字典。
   写成字典时 `actool` 不报错、`Assets.car` 里有图，但主屏是白块（CI run #31）。
2. **`Assets.car` 存在 ≠ 图标进包**。少写 `ASSETCATALOG_COMPILER_APPICON_NAME`
   时 car 照样生成，只是空的。

---

## 协议参考

- UPnP Device Architecture 1.0
- DLNA Networked Device Interoperability Guidelines (DMR-1.50)
- `suaptv/UnPlay` 的 HTTP API 文档

## 许可

仅供学习与个人使用。DLNA/UPnP 为标准公开协议，本项目为独立实现。