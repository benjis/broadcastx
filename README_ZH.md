# BroadcastX

<a href="README.md">🇬🇧 English</a> | <a href="README_ZH.md">🇨🇳 中文版</a>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/) [![PyPI](https://img.shields.io/pypi/v/broadcastx.svg)](https://pypi.org/project/broadcastx/)

发现、监控并下载 X/Twitter 用户时间线中的直播视频。

**BroadcastX** 是一款命令行工具，支持以下功能：

- **扫描** — 查找用户时间线中的直播链接
- **下载** — 下载直播视频，并自动修正手机旋转方向
- **监控** — 监控用户主页，检测直播开始，自动下载回放

## 功能特性

### 扫描

使用 Playwright 浏览器自动化工具，滚动浏览用户的 X 主页，拦截 GraphQL API 响应以提取直播 URL。比 DOM 解析更稳定可靠。

### 下载 + 自动旋转修正

通过 `yt-dlp` 下载直播视频，并基于 HLS 流中嵌入的 timed-ID3 元数据自动修正手机方向。竖屏录制的直播在下载后会自动调整为正确方向。同时会生成 `.rotation.jsonl` 侧车文件供检查使用。

### 监控

持续监控用户主页。检测到直播时，定期检查直播状态。直播结束后自动下载回放。


## 前置依赖

- **Python 3.11+**
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — `brew install yt-dlp`
- **[ffmpeg](https://ffmpeg.org/)** — `brew install ffmpeg`
- **Google Chrome**（需单独安装）

## 安装

```bash
# 安装 BroadcastX 及其依赖
uv sync

# 安装 Playwright 的浏览器驱动
uv run playwright install chromium
```

## 快速开始

```bash
# 扫描用户时间线，查找直播链接
broadcastx scan @username

# 同时扫描用户自己发布的视频和照片（排除普通转发）
broadcastx scan @username --all

# 从扫描结果下载直播
broadcastx download --from output/username/broadcasts.json

# 监控 broadcastx.toml 中的全部用户
broadcastx monitor
```

## 配置文件

将 [`broadcastx.example.toml`](broadcastx.example.toml) 复制为运行目录下的
`broadcastx.toml`。BroadcastX 会自动发现它；只有配置文件位于其他位置时
才需要使用 `--config PATH`。

```toml
output_root = "output"

[monitor]
check_interval_seconds = 1800
live_interval_seconds = 300
download = true

[download]
timezone = "UTC"
datetime_format = "%Y-%m-%d_%H-%M-%S"
filename_template = "{datetime}_{broadcast_id}"
video_filename_template = "{datetime}_{tweet_id}_{media_id}"

[[users]]
username = "Alice"
timezone = "Australia/Sydney"

[[users]]
username = "Bob"
timezone = "Asia/Ho_Chi_Minh"
```

优先级为：明确传入的 CLI 参数、用户专属配置、命令全局配置、内置默认值。
相对的 `output_root` 从配置文件所在目录开始解析。

## 使用说明

### 扫描时间线

```bash
broadcastx scan @username

# 扫描直播、原创视频和原创照片，并排除普通转发
broadcastx scan @username --all

# 可选参数：
#   --max-scrolls 100      最大滚动次数
#   --scroll-delay 2.0     滚动间隔（秒）
#   --idle-timeout 10.0    无新数据时停止等待时间（秒）
#   --output FILE          覆盖输出路径（默认：output/<用户>/broadcasts.json）
#   --headless             无头模式运行浏览器
```

扫描器会打开用户的 X 主页，向下滚动时间线并拦截 API 响应。直播 URL 从推文卡片中提取。如果未登录，浏览器会显示登录页面——手动登录后在终端按 Enter 继续。会话信息保存在 `~/.broadcastx/chrome-profile/` 中，供后续使用。

使用 `--all` 时，同一个 `broadcasts.json` 还会包含 `media` 数组。视频以 Post URL 保存，让下载器获取该 Post 内的全部视频；照片保存为 X 的直接媒体 URL。Reply 会包含，普通 Repost 会排除；Quote Post 只包含目标用户自己附加的媒体，不包含被引用 Post 的媒体。

### 下载直播和普通 Post 媒体

```bash
# 单一直播
broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl

# 普通 Post：下载其中全部视频或照片，也支持 twitter.com 链接
broadcastx download https://x.com/username/status/1234567890123456789

# 混合下载直播和普通推文
broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl https://x.com/username/status/1234567890123456789

# 从扫描结果批量下载直播、视频和照片
broadcastx download --from output/username/broadcasts.json

# 多并发下载
broadcastx download --from output/username/broadcasts.json -p 3

# 自定义输出根目录
broadcastx download --from output/username/broadcasts.json -o ./archive

# 使用 Firefox cookies
broadcastx download --from output/username/broadcasts.json --browser firefox

# 显示 yt-dlp 详细信息
broadcastx download --from output/username/broadcasts.json -v
```

BroadcastX **自动修正手机旋转方向**：如果直播 HLS 流中包含方向元数据，视频将被重新编码以正确的方向播放。

直接下载 URL 时，BroadcastX 会从 yt-dlp metadata 读取 uploader ID 和直播
时间，自动选择对应用户的配置与输出目录。无法识别用户时使用全局配置并写入
`output/unknown/videos/`。

普通视频 Post 使用独立的 `video_filename_template`，可在 `[download]` 或
`[[users]]` 中设置。允许 `{datetime}`、`{tweet_id}`、`{media_id}`，必须包含
两个 ID。时区和时间格式沿用现有配置；缺少时间时使用 `<tweet_id>_<media_id>`，
缺少媒体 ID 时使用稳定的媒体序号。视频保存在
`<output_root>/<user>/videos/`，照片保存在
`<output_root>/<user>/photos/`。已存在文件直接跳过，不覆盖。
`/video/N` 链接也会下载整条推文的全部视频。

`--from` 支持混合 URL 数组或 `{"url": "..."}` 对象数组。非法链接、无原生视频、
纯图片 Post URL 会被自动解析并下载其中属于该 Post 的全部照片。混合媒体 Post
先下载全部视频，再下载全部照片，并将所有文件放在同一个结果中。
权限不足等情况会明确报告失败，并继续其他下载；任何失败使命令返回非零退出码，
已成功的文件会保留。不跟随外部视频网站链接。直播旋转校正保持现有行为，
普通 Post 不生成旋转侧车文件。monitor 仍仅发现 Broadcast；scan 只有在传入
`--all` 时才额外发现普通 Post 媒体。

对于扫描生成的 JSON，`download --from` 先读取 `broadcasts`，再读取 `media`，
并按第一次出现的位置去重。默认的 `--parallel 1` 严格按这个顺序逐一下载；使用
`--parallel N` 时完成提示可能乱序，但返回结果仍保持来源顺序。

### 监控直播

```bash
# 监控单个用户
broadcastx monitor @username

# 同时监控多个用户（共享一个 Chromium 配置文件）
broadcastx monitor @SpaceX @NASA @elonmusk

# 监控 broadcastx.toml 中的全部 [[users]]
broadcastx monitor

# 只选择 Bob，同时继续应用 Bob 的专属配置
broadcastx monitor Bob

# 单次检测（不循环）
broadcastx monitor @username --once

# 使用自定义输出根目录
broadcastx monitor @username --output-dir ./archive

# 自定义检测间隔（秒）
broadcastx monitor @username --check-interval 1800 --live-interval 300

# 仅检测，不下载
broadcastx monitor @username --no-download
```

监控器循环运行：

1. **主页检查**（每隔 `check-interval` 秒，默认 30 分钟）—— 打开每个用户的主页，查找直播卡片
2. **直播检测** —— 找到候选后判断是否为当前直播
3. **状态检查**（每隔 `live-interval` 秒，默认 5 分钟）—— 持续检查直到直播结束
4. **自动下载** —— 直播结束后自动下载回放

多个用户共享一个 Chromium 配置文件，只需登录一次。

事件按用户记录到 `output/<用户名>/monitor_events.json`。

## 输出目录结构

```
output/
├── alice/
│   ├── broadcasts.json
│   ├── monitor_events.json
│   └── videos/
│       ├── 2026-08-28_22-00-00_[id].mp4
│       └── 2026-08-28_22-00-00_[id].rotation.jsonl
└── bob/
    └── ...
```

## 管线示例

```bash
# 扫描 + 下载所有找到的直播
broadcastx scan @username
broadcastx download --from output/username/broadcasts.json

# 监控并自动下载
broadcastx monitor @username --output-dir ./archive

```

## 工作原理

### 扫描器
使用 Playwright 拦截 X 页面自身产生的 timeline GraphQL 响应。扫描器通过目标用户的 post ID 判断进度，只从能够确认属于目标用户的 post 中提取直播链接，并且不依赖固定的 GraphQL operation 名称。

### 下载器
封装 `yt-dlp`（内置 `TwitterBroadcastIE` 提取器），并增加以下功能：
- **旋转侧车提取** —— 从 HLS 片段解析 timed-ID3 元数据
- **自动旋转修正** —— 通过 ffmpeg 重新编码视频，纠正方向

### 旋转侧车文件
JSONL 侧车文件与 MP4 使用相同的文件名 stem，每条记录对应一个 HLS 片段：
- `raw_rotation` —— Periscope 的原始传感器角度
- `rotation` —— 量化后的方向：0°、90°、180° 或 270°（带迟滞处理）
- `ntp` —— NTP 时间戳，用于时间线重建

## 开源协议

MIT
