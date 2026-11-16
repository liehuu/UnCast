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

### 方式一：GitHub Actions 出 tipa（推荐）

1. 把本目录推到 GitHub 仓库
2. Actions 会自动跑 `Build UnCast tipa`
3. 下载 artifact `UnCast-tipa`
4. 传到 Apple TV，用 TrollStore 打开安装

### 方式二：本地 Mac

```bash
brew install xcodegen
cd UnCast
xcodegen generate
open UnCast.xcodeproj
# Xcode 里选你的 Apple TV 跑
```

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