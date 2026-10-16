# BroadcastX

<a href="README.md">🇬🇧 English</a> | <a href="README_ZH.md">🇨🇳 中文版</a>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/) [![PyPI](https://img.shields.io/pypi/v/broadcastx.svg)](https://pypi.org/project/broadcastx/)

发现、监控并下载 X/Twitter 直播和普通 Post 媒体。

**BroadcastX** 是一款命令行工具，支持以下功能：

- **扫描** — 查找直播链接，使用 `--all` 还可扫描普通 Post 视频和照片
- **下载** — 下载直播并自动修正手机旋转方向，也支持普通 Post 视频和照片
- **监控** — 推荐使用 `homemonitor` 从 X Home 发现直播并自动下载回放；原有 `monitor` 继续可用

## 功能特性

### 扫描

使用 Playwright 浏览器自动化工具，滚动浏览用户的 X 主页，拦截 GraphQL API 响应以提取直播 URL。比 DOM 解析更稳定可靠。

### 下载 + 自动旋转修正

通过 `yt-dlp` 下载直播视频，并基于 HLS 流中嵌入的 timed-ID3 元数据自动修正手机方向。竖屏录制的直播在下载后会自动调整为正确方向。同时会生成 `.rotation.jsonl` 侧车文件供检查使用。

### 监控

日常监控推荐使用 **`broadcastx homemonitor`**。它定期加载当前登录账号的
X Home（首页）**Live on X** 列表，再筛选指定用户，无需逐个扫描用户的个人主页。
这样可以省去逐用户的时间线扫描，目的是降低网络开销，尤其适合监控多个用户；
目前没有量化的带宽基准数据。已发现的直播从一次成功加载的列表中消失时，立即开始下载回放。

发现范围取决于 X 向当前账号展示的 Home 列表，无法保证覆盖所有指定用户。
原有 **`broadcastx monitor`** 继续支持逐用户扫描个人主页，并单独检查直播状态。


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

# 推荐：通过 X Home 监控 broadcastx.toml 中的全部用户
broadcastx homemonitor

# 不使用配置文件，直接指定用户
broadcastx homemonitor @username
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

`homemonitor` 使用 `[[users]]`、`output_root`、下载命名配置，以及
`[monitor].browser` 和 `[monitor].download`。它默认每 **120 秒**检查一次，
默认显示浏览器窗口；通过 `--check-interval` 和 `--headless` 修改。
`[monitor]` 中的 `check_interval_seconds`、`live_interval_seconds` 和 `headless`
只用于原有 `monitor` 命令。目前没有 `[homemonitor]` 配置段。

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

`--from` 支持混合 URL 数组或 `{"url": "..."}` 对象数组。
纯图片 Post URL 会被自动解析并下载其中属于该 Post 的全部照片。混合媒体 Post
先下载全部视频，再下载全部照片，并将所有文件放在同一个结果中。
非法链接、无原生媒体或权限不足等情况会明确报告失败，并继续其他下载；任何失败使命令返回非零退出码，
已成功的文件会保留。不跟随外部视频网站链接。直播旋转校正保持现有行为，
普通 Post 不生成旋转侧车文件。monitor 仍仅发现 Broadcast；scan 只有在传入
`--all` 时才额外发现普通 Post 媒体。

对于扫描生成的 JSON，`download --from` 先读取 `broadcasts`，再读取 `media`，
并按第一次出现的位置去重。默认的 `--parallel 1` 严格按这个顺序逐一下载；使用
`--parallel N` 时完成提示可能乱序，但返回结果仍保持来源顺序。

### 从 X Home 监控直播（推荐）

```bash
# 监控配置中的全部用户，默认每两分钟检查一次
broadcastx homemonitor

# 只监控指定用户
broadcastx homemonitor @SpaceX @NASA

# 单次检测；等待本轮触发的下载完成后退出
broadcastx homemonitor --once

# 无头模式：读取 Home 页面已有的 fleetline 响应
broadcastx homemonitor --headless

# 修改检查间隔，或只检测不下载
broadcastx homemonitor --check-interval 180
broadcastx homemonitor @username --no-download

# 使用其他配置文件中的用户和 output_root
broadcastx homemonitor --config ./archive.toml
```

首次使用请先以可见模式运行，按提示登录 X。会话保存在
`~/.broadcastx/chrome-profile/`；无头模式需要已有的登录会话。
可见模式打开 **Live on X** 中的 Broadcast 卡片，无头模式读取 Home 的
`fleetline` 响应，两者都忽略 Spaces，再按配置或 CLI 指定的用户筛选。
指定用户名不会强制 X 在 Home 展示该用户，因此发现范围受当前账号的 Home 列表限制。

新发现的直播链接会显示在终端，并记录到 `output/homemonitor_events.json`。
活动直播链接原子保存到 `output/homemonitor_state.json`，重启后可以恢复。
某个链接从一次成功加载的列表中消失时，立即触发下载，不再额外确认直播是否结束。
下载使用对应用户的命名配置，保存到 `output/<用户名>/videos/`。
Home 检查未完成或下载失败时保留状态；下载成功后才清除，使用 `--no-download`
时则在链接消失后清除。检测相关的 HTTP 429 会使进程停止；无关的
`ViewerBadgeCounts` 429 只报告并忽略。每轮检查后关闭临时浏览器。

### 扫描个人主页监控直播（备选）

原有命令继续支持。日常使用推荐 `homemonitor`；需要直接扫描个人主页、
不依赖 Home 展示范围时，可以使用 `monitor`。

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
├── homemonitor_events.json
├── homemonitor_state.json
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

# 推荐：监控并自动下载（输出根目录取自配置中的 output_root）
broadcastx homemonitor @username

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
