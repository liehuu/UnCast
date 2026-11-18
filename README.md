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

仓库 `dist/UnCast-1.0.1.tipa` 就是可直接安装的成品（由 GitHub Actions 构建）。

| 项 | 值 |
|---|---|
| 版本 | 1.0.1（build 2） |
| 大小 | 3,667,587 字节（3.50 MB） |
| sha256 | `83e1bfb422a58299bc28e44e52f21615157a21d909830004c3a562a0274c9566` |
| 架构 | arm64 |
| 平台 | tvOS（`LC_BUILD_VERSION.platform = 3`，minos 15.0） |
| 加密 | 未加密（`cryptid = 0`） |
| 签名 | ad-hoc（含 `LC_CODE_SIGNATURE`，dataSize > 0） |
| 多播权限 | `com.apple.developer.networking.multicast = true` |
| 主屏图标 | 内置，橘黄渐变主题（`Assets.car` 3.17 MB，9 条 rendition） |
| 构建 | GitHub Actions run #44216056335（`df995044`），18 步全绿 |

上述指标是读 tipa 内二进制的原始字节实测得出，可用
`python3 scripts/check_tipa.py <tipa>` 自行复现（16 项断言）。

**1.0.1 修的是遥控器交互**：方向键上下呼出进度条、进度条下左右快进/快退、
进度条下再按下键展开按钮行。完整说明见下文「遥控器交互」。

> 直接从 1.0.0 覆盖安装即可，不需要先卸载 —— bundle id 未变，
> 设置与播放记录都会保留。

> 包体从 1.67 MB 涨到 3.50 MB，是因为图标从深色单调渐变改成了
> 三色高饱和渐变加高光 —— 画面越复杂 PNG 压缩率越低。3.5 MB 对
> TrollStore 安装没有影响，主屏显示也只会用其中最小的那档（400×240）。

> **图标不需要你自己提供。** `scripts/make_icon.py` 用纯 Python（只依赖 numpy）
> 按 tvOS 规范生成全部 13 张图：主屏图标三层的 @1x/@2x（400×240 / 800×480）、
> App Store 图标 1280×768、Top Shelf 常规与宽屏的 @1x/@2x。

#### 图标配色（内置四套，可一条命令切换）

当前用的是 **lava 熔岩**（柠檬黄 → 橙 → 玫红）。配色集中在
`make_icon.py` 的 `THEMES` 字典里，每套四个色位：渐变三段、外发光、字形渐变、边缘亮线。

| 主题 | 观感 |
|---|---|
| `lava` | 柠檬黄 → 橙 → 玫红（**当前默认**） |
| `aurora` | 青 → 靛紫 → 品红 |
| `mint` | 青绿 → 湖蓝 → 宝蓝 |
| `violet` | 品红 → 紫罗兰 → 电光蓝 |

```bash
python3 scripts/icon_themes_preview.py     # 先出四套对比图 dist/theme-preview.png
python3 scripts/make_icon.py --theme mint  # 换成 mint
python3 scripts/check_icon.py .            # 34 项校验
```

想完全自定义：改 `THEMES` 里某套的三个渐变色值即可（都是 0–1 的 RGB 浮点）。
不需要美术资源，也不需要 Xcode。

设计上三个容易踩的点（都已在代码注释里说明）：

1. **发光要衬托轮廓，不能模糊轮廓** —— 外发光强度 0.24 / 半径 20px 是上限，
   再大描边的锐利度就被糊掉了。
2. **质感靠光，不靠纹理** —— 试过 45° 斜纹，在 1280×768 上是摩尔纹噪点，显脏。
3. **字形要占满** —— 占画幅 56%×57%。低于 50% 在 tvOS 主屏的小格子里会显得空。

### TrollStore 安装步骤

1. 把 `UnCast-1.0.0.tipa` 传到 iPhone / iPad，或用「文件」App 存到 iCloud Drive
2. 在 Apple TV 上打开 TrollStore → **Install from File**（或通过 Filza / 局域网上传）
3. 选中 `UnCast-1.0.1.tipa` → 安装
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

## 遥控器交互（分级控制层 · v1.0.1 重做）

v1.0.0 交付后实测报了两个症状，看起来是两件事，其实是**同一个根因**：

> 方向键不能控制播放进度了；进度条一旦自动隐藏就再也调不出来。

### 根因：`onMoveCommand` 是死代码

v1.0.0 把方向键处理挂在 SwiftUI 的 `onMoveCommand` 上：

```swift
// v1.0.0 的写法 —— 静态看完全正确，运行时一次都不执行
case .up, .down:
    revealControls(focusPlay: true)
```

`onMoveCommand` **依赖焦点**。控制层隐藏时 `focus = nil`，而承载视频的
`UIViewRepresentable` 当时 `canBecomeFocused = false` —— 整棵视图树里
没有任何可聚焦元素。tvOS 的焦点引擎找不到派发目标，方向键直接丢弃，
那段 `case .up, .down` 永远不会执行。

于是：「进度条调不出来」＝ 按键进不来；「方向键控不了进度」＝ 同一个按键
进不来。**一个根因，两个症状。** 这类缺陷编译期、静态审阅都发现不了 ——
分支逻辑写着，只有真机上按下去毫无反应才暴露。

### 解法：常驻焦点锚点 + 三级累进

**① 画面层自己当焦点锚点，并且用 `pressesBegan` 直接抓按键**（不依赖焦点派发）：

```swift
final class PlayerContainerView: UIView {
    var capturesKeys = true
    var onKey: ((KeyEvent) -> Void)?

    // 动态：按钮态必须让位，否则全屏锚点一直霸占焦点，按钮永远聚焦不上
    override var canBecomeFocused: Bool { capturesKeys }

    // 不要焦点光晕（tvOS 默认给可聚焦 view 画一圈白边 + 缩放）
    override var focusEffect: UIFocusEffect? { nil }

    override func pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        guard capturesKeys else {          // 按钮态一律放行给焦点引擎
            super.pressesBegan(presses, with: event)
            return
        }
        for press in presses {
            guard let type = press.type else { continue }
            switch type {
            case .upArrow:    onKey?(.move(.up));    return
            case .downArrow:  onKey?(.move(.down));  return
            case .leftArrow:  onKey?(.move(.left));  return
            case .rightArrow: onKey?(.move(.right)); return
            case .playPause:  onKey?(.playPause);    return
            case .menu:       onKey?(.menu);         return
            default: continue
            }
        }
        super.pressesBegan(presses, with: event)
    }
}
```

**② 三级累进状态机**，每级按键含义不重叠：

```swift
enum Level: Int, Comparable {
    case hidden = 0      // 只有画面
    case progress = 1    // 进度条 + 时间
    case buttons = 2     // 进度条 + 按钮行
    static func < (a: Level, b: Level) -> Bool { a.rawValue < b.rawValue }
}

@State private var level: Level = .progress
```

| 当前状态 | 按 ↑ | 按 ↓ | 按 ← / → |
|---|---|---|---|
| 看片（hidden） | → 进度条 | → 进度条 | seek（连按累进） |
| 进度条（progress） | → 收起 | → **展开按钮行** | seek（连按累进） |
| 按钮（buttons） | **→ 退回进度条** | → 收起控制层 | 按钮间移动（焦点引擎接管） |

三条交互逐一对应：上下键都能呼出进度条 ✅；进度条下左右键快进/快退 ✅；
进度条下再按下键出现「上一首 / 下一首 / 暂停 / 投屏停止」按钮行 ✅；
按钮行按上退回进度条（按钮行收起、进度条留下）✅。

**按钮态的上键为什么写在 `onMoveCommand` 里**：按钮态锚点的
`canBecomeFocused` 已关、`pressesBegan` 直接放行，方向键由焦点引擎
派发给 SwiftUI。写进 `handleMove` 会是死代码 —— 一次都不会执行。

### 连按累进 seek

按一次跳 15 秒，连按累进到 1 分钟、5 分钟：

| 连按次数 | 累计跳转 | 提示显示 |
|---|---|---|
| 1 | 15 秒 | `+15s` |
| 2 | 1 分钟 | `+1:00` |
| 3 | 5 分钟 | `+5:00` |
| 4 | 10 分钟 | `+10:00` |
| 5 | 15 分钟 | `+15:00` |

三条规则：

- **同方向窗口内连按才累进**。窗口 1.0 秒（遥控器连按手感区间 0.6~1.2s），
  停顿超窗口或换方向都重新从 15 秒起算。
- **第 3 次之后继续递进**，每多按一次再加 5 分钟。停住的话用户第 4 次
  按下会看到同一个 `+5:00`，会以为按键失灵。
- **绝对定位，不是相对累加**。`seek` 是异步的，连按时上一次 seek 可能
  还没落地、`engine.currentTime` 仍是旧值。若按「再 +45 秒」累加会基于
  旧位置计算，跳转量偏小。所以记住连按序列的**起点**（`seekAnchor`），
  每次算 `起点 + 累计偏移`，结果与按键速度无关。

按钮行的快退/快进按钮走同一个入口，连点按钮同样累进。

**③ 两态互斥，不做运行时仲裁**。按钮态把 `capturesKeys` 与
`canBecomeFocused` 一起关掉，焦点整块交给 SwiftUI 的 `@FocusState`；
退出按钮态时再打开，并主动要一次焦点：

```swift
// updateUIView 里
let wasCapturing = uiView.capturesKeys
uiView.capturesKeys = capturesKeys
if capturesKeys && !wasCapturing {
    uiView.setNeedsFocusUpdate()      // 不要这一次，控制层收起了但方向键仍无反应
    uiView.updateFocusIfNeeded()
}
```

**④ 三条兜底**，防止再次掉进无焦点死区：

- `didMoveToWindow` 首次上屏主动要焦点（否则第一下按键丢失）
- `.onChange(of: focus)`：按钮态下焦点意外变 nil → 退回进度条态
- `restartHide()` 的三条守卫：正在播放 && 无按钮焦点 && 检查器未开 才自动隐藏

### 为什么这一版值得信

`scripts/verify_player.py` 有 **55 条**断言，`scripts/verify_player_injection.py`
逐条注入破坏（**25 项变异 + 1 项 plist 格式**），确认每条断言真的能失败。
其中 4 条是 v1.0.0 那批「假 PASS」的教训：

| 变异 | 旧断言为什么抓不住 | 新判据 |
|---|---|---|
| 按钮内部存 `@State` 焦点 | 查的是 `"@FocusState" not in btn`，`@State` 不含这个子串 | 列出全部可变属性包裹器，一个不许有 |
| 下键不展开按钮层 | 查 `".buttons" in down_body`，`case .buttons:` **标签本身**就命中 | 按花括号深度切出 `.progress` 那一格再找赋值 |
| 上键隐藏态不呼出 | 同上，`case .progress, .buttons:` 含 `.progress` | 精确切到 `.hidden` 那一格 |
| 切回锚点态不请求焦点 | 查全文有无 `setNeedsFocusUpdate`，`didMoveToWindow` 里还有一处 | 只在 `updateUIView` 函数体内找 |

一次 `python3 scripts/verify_player.py && python3 scripts/verify_player_injection.py`
就能复现全部结论。

### 一条更深的教训：断言写错比代码写错更致命

第一版修完后本机 Gate 全绿，CI 编译却失败，一次报三条：

```
error: cannot override mutable property with read-only property 'focusEffect'
error: 'UIFocusEffect' is unavailable in tvOS
error: initializer for conditional binding must have Optional type,
       not 'UIPress.PressType'
```

根因不是手误。是我在断言里写了「`focusEffect` 必须为 `nil`」，
而 `UIFocusEffect` 在 tvOS 上是 `API_UNAVAILABLE(tvos)`（只有 iOS 15+ 提供），
`UIView.focusEffect` 在 tvOS 上也不可覆写 —— **在 tvOS 上唯一能满足
那条断言的写法，恰好是编译不过的写法**。断言把源码逼进了死角。

于是加了一整层前置校验，跑在编译之前：

| 脚本 | 作用 |
|---|---|
| `scripts/check_tvos_api.py` | 4 条 tvOS API 可用性规则，每条注明 SDK 头文件出处 |
| `scripts/selftest_check_tvos_api.py` | 注入上面那三条报错原文做反向验证（5 项） |

写第一版规则时把 `focusEffectDisabled` 写成 `\.focusEffectDisabled\(`，
只认链式调用、漏了裸调用形态；反向验证的 M4 当场变红，抓出了这条
**永远为绿的规则**。这就是反向验证存在的意义。

现在 CI 是 18 步：Gate 0（tvOS API）→ 编译 → 打包 → Gate 1（交互契约
+ 变异测试）→ Gate 2（图标进包）→ Gate 3（产物可装）。

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
配色、主题切换与设计取舍见上文「图标配色」一节。

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
5. **只做 DLNA，不做 AirPlay 接收端**（结构性限制，无法绕过）。详见下一节。

---

## 为什么有些 App 的投屏列表里没有这台电视

这是最常被问到的问题，而且答案分两类，处理方式完全不同 —— **先分清是哪一类，再动手**。

### 类别 A：对方 App 根本不发 DLNA 搜索（改不了）

很多国内 App 的「投屏」按钮背后不是 DLNA，而是三种别的东西：

| 实际机制 | 特征 | 谁能收到 |
|---|---|---|
| **AirPlay**（`AVPlayerViewController` / `MPVolumeView` 的路线选择器） | 列表里出现的是 HomePod、Apple TV 等苹果设备 | tvOS 系统自带的 AirPlay 接收端 |
| **私有联播码**（App 厂商自家的电视端 App） | 弹窗要求「输入电视联播码」或扫码，需要装同名电视端 App | 该厂商的电视端 App，与协议无关 |
| **Chromecast** | 列表里出现 Chromecast / Google TV | 支持 Cast 协议的设备 |

这三类都**不会发 `M-SEARCH`**，所以它们在协议层就看不见任何 DLNA 设备。
不是 UnCast 的问题，原版 UnPlay 同样看不见 —— 因为 UnPlay 也是 DLNA 接收端。

**为什么不能自己实现 AirPlay 接收端**：AirPlay 接收需要系统级的
`AirPlayReceiver` 能力与 FairPlay 密钥链，苹果只授权给 tvOS 内置接收端和
少数 MFi 厂商。第三方 App（包括 TrollStore 安装的）拿不到这条路。
所以「用 AirPlay 投过去」这件事，走的是 tvOS 自己，跟本应用无关。

**后果要说清楚**：走 AirPlay 时，手机是推流方。手机锁屏、息屏、
切到别的 App 之后音频会话会被系统回收，播放随之中断。
「手机锁屏、电视继续放」这个保证**只在 DLNA 路径上成立**
（因为 DLNA 是电视自己拉流，手机只下指令）。

### 类别 B：对方发 DLNA 搜索但没发现我们（能修，已经修了）

这一类才是真 bug。本次修复的四个缺陷都属于这一类 ——
它们全都能编译通过、本地跑也不报错，只在对着严苛客户端时才表现为「搜不到」：

| 缺陷 | 症状 | 修法 |
|---|---|---|
| `ST` 原样回显 | 日志里有 `M-SEARCH`，手机上什么都没有 | `ssdp:all` 展开成 6 条各自回复 |
| 没有周期通告 | 刚启动能看到，过一阵子就消失，重启 App 又出现 | 每 `max-age/2`（15 分钟）重发 alive |
| 缺裸 UDN 通告 | 部分 Cling 系客户端完全看不见 | 补 `uuid:<UDN>` 这一条，且 USN 不加 `::` 后缀 |
| `BOOTID` 写死 1 | App 重启后客户端连不上（还在用旧缓存） | 持久化递增，并补 `DATE` 头 |

### 怎么一次分清 A 和 B

设置页新增了「投屏兼容性」一栏，有四个读数：

```
SSDP 搜索请求   0 次     ← 关键指标
已回复设备      0 条
通告次数        6 次
支持的标准      DLNA / UPnP · 仅此一种
```

操作步骤：**让那个 App 的投屏列表保持打开**（列表打开时它才会持续搜索），
盯着「SSDP 搜索请求」这个数字看 10 秒。

- **数字一直是 0** → 类别 A。它没在找 DLNA 设备，换任何 DLNA 接收端都一样。
  这个 App 只能走它自己支持的方式（AirPlay / 联播码 / 别的）。
- **数字在涨，但它列表里还是没有** → 类别 B，是我们的问题。
  这时看「投屏日志」里 `M-SEARCH` 那几行记的 `ST=` 值，对照上表定位。

> 这个计数器不是调试残留。没有它，用户对「搜不到」只能靠猜，
> 而猜测的默认结论往往是「这个 App 坏了」或「这软件不行」——
> 两种结论都不指向真正原因。

---

## 排查

界面里「投屏日志」能看到完整链路：

- 看不到 `M-SEARCH` → 对方 App 不发 DLNA 搜索，见上一节类别 A
- 有 `M-SEARCH` 但设备不出现 → 看日志里记的 `ST=` 值；应为 6 条回复
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
│   ├── icon_themes_preview.py     # 四套配色对比图，换色先看图再决定
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

想改图标配色：`python3 scripts/icon_themes_preview.py` 先看四套效果，
再 `python3 scripts/make_icon.py --theme <名字>` 切换，最后
`python3 scripts/check_icon.py .` 确认。想完全自定义就改 `THEMES` 里的色值。

---

## 三道门禁在挡什么

CI 里 `BUILD SUCCEEDED` 只是必要条件，不是充分条件。这个项目踩过的坑里，
有一半是「编译全绿、装上不对」。所以有三道独立的门禁，每道都做过反向注入验证
（故意把缺陷放回去，确认它真的会红）：

| 门禁 | 检查对象 | 断言数 | 挡住的典型缺陷 |
|---|---|---|---|
| Gate 1 | 源码 + 图标源资源 | 36 + 34 + 17 + 12 + 37 + 17 | 方向键冲突、控制层唤不回、图标尺寸/透明度错、SSDP 报文不合规、assert 永真 |
| Gate 2 | 编译产物 `Assets.car` | 11 | `ASSETCATALOG_COMPILER_APPICON_NAME` 漏配 → 图标不进包 |
| Gate 3 | tipa 字节 | 16 + 20 | 平台不是 tvOS、被 FairPlay 加密、缺多播权限、空签名区 |

其中两条是本项目真实踩过的、且**构建阶段完全不报错**的：

1. **tvOS 的 `CFBundlePrimaryIcon` 必须是字符串**，不是 iOS 那种字典。
   写成字典时 `actool` 不报错、`Assets.car` 里有图，但主屏是白块（CI run #31）。
2. **`Assets.car` 存在 ≠ 图标进包**。少写 `ASSETCATALOG_COMPILER_APPICON_NAME`
   时 car 照样生成，只是空的。

### 怎么证明「修复真的进了包」

门禁全绿只证明「源码符合契约」和「包能装」，**不证明修复在包里**。
这两件事必须分开验证，否则会出现「门禁 37/37、设备上还是老行为」的假象。

可靠做法是**差分两个产物**，而不是在单个包里找字符串：

```python
old = binary_of('dl35/UnCast.tipa')   # 修复前那次的产物
new = binary_of('dl36/UnCast.tipa')   # 修复后那次的产物
for sym in ['replyTargets', 'startNotifyLoop', 'bump', 'stMatches']:
    # 新函数只应出现在新包里，被删的旧函数只应出现在旧包里
    print(sym, old.count(sym.encode()), new.count(sym.encode()))
```

实测结果（run #35 → #36）：

| 符号 / 字面量 | 旧包 | 新包 | 含义 |
|---|---|---|---|
| `replyTargets` | 0 | 2 | 新函数，仅在修复后 |
| `startNotifyLoop` | 0 | 3 | 新函数，仅在修复后 |
| `stMatches` | 1 | 0 | 被替换掉的旧函数 |
| `Darwin/1.0 UPnP/1.1 UnCast/1.0` | 0 | 1 | 新 SERVER 串 |
| `UPnP/1.0 DLNADOC/1.50` | 2 | 1 | 旧串，残留一处 → 见下 |

#### 判据本身的两个坑（都真实踩过）

1. **`count()` 是字面量匹配，不是正则**。我一度把 `rb'NOTIFY \* HTTP/1\.1'`
   直接喂给 `count()`，转义符被当成真实字符，命中恒为 0 —— 于是「修复没进包」
   的错误结论。要用 `re.search` 才写正则。
2. **`<= 15 字节`不落 cstring 的猜想不成立**。Swift 有短字符串优化，
   `M-SEARCH`（8 字节）、`239.255.255.250`（15 字节）确实查不到；
   但 `upnp:rootdevice`（15 字节）却查到了 —— 有反例，猜想作废。
   所以**不能**用「某个短字面量搜不到」推断「代码没编进去」。
   可靠判据只有差分 + 符号名，符号名不会因字符串优化消失。

上面表格最后一行（旧 SERVER 串 2 → 1 而非 0）就是靠差分查出来的真问题：
改 SSDP 的 `serverString` 时漏了 `HTTPServer` 那处，导致同一版包里
SSDP 自报 UPnP/1.1、HTTP 自报 UPnP/1.0 —— 严格客户端会当成两台设备。
已修（HTTP 复用同一常量），并补了 3 条断言 + 1 条注入。

---

## 协议参考

- UPnP Device Architecture 1.0
- DLNA Networked Device Interoperability Guidelines (DMR-1.50)
- `suaptv/UnPlay` 的 HTTP API 文档

## 许可

仅供学习与个人使用。DLNA/UPnP 为标准公开协议，本项目为独立实现。