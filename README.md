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
| 大小 | 280,642 字节（0.27 MB） |
| sha256 | `ec3b5738c9e209498ea045d41178cfe649e2c34fb24a4aebba3dcecfa0cf07d5` |
| 架构 | arm64 |
| 平台 | tvOS（`LC_BUILD_VERSION.platform = 3`，minos 15.0） |
| 加密 | 未加密（`cryptid = 0`） |
| 签名 | ad-hoc（含 `LC_CODE_SIGNATURE`） |
| 多播权限 | `com.apple.developer.networking.multicast = true` |

下载：<https://github.com/liehuu/UnCast/releases/download/v1.0.0/UnCast-1.0.0.tipa>

上述指标是读 tipa 内二进制的原始字节实测得出，可用
`python3 scripts/check_tipa.py <tipa>` 自行复现（16 项断言）。

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

## 关键技术点

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
├── .github/workflows/build.yml    # CI：出未签名 tipa
└── UnCast/
    ├── App/
    │   ├── UnCastApp.swift        # 入口
    │   ├── Info.plist
    │   └── UnCast.entitlements    # 组播权限
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

## 协议参考

- UPnP Device Architecture 1.0
- DLNA Networked Device Interoperability Guidelines (DMR-1.50)
- `suaptv/UnPlay` 的 HTTP API 文档

## 许可

仅供学习与个人使用。DLNA/UPnP 为标准公开协议，本项目为独立实现。