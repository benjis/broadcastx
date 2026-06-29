# BroadcastX

<a href="README.md">🇬🇧 English</a> | <a href="README_ZH.md">🇨🇳 中文版</a>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

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
# 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装 BroadcastX 及其依赖
pip install -e .

# 安装 Playwright 的浏览器驱动
playwright install chromium
```

## 快速开始

```bash
# 扫描用户时间线，查找直播链接
broadcastx scan @username

# 从扫描结果下载直播
broadcastx download --from output/broadcasts.json

# 监控用户直播状态
broadcastx monitor @username
```

## 使用说明

### 扫描时间线

```bash
broadcastx scan @username

# 可选参数：
#   --max-scrolls 100      最大滚动次数
#   --scroll-delay 2.0     滚动间隔（秒）
#   --idle-timeout 10.0    无新数据时停止等待时间（秒）
#   --output FILE          输出路径（默认：output/broadcasts.json）
#   --headless             无头模式运行浏览器
```

扫描器会打开用户的 X 主页，向下滚动时间线并拦截 API 响应。直播 URL 从推文卡片中提取。如果未登录，浏览器会显示登录页面——手动登录后在终端按 Enter 继续。会话信息保存在 `~/.broadcastx/chrome-profile/` 中，供后续使用。

### 下载直播

```bash
# 单一直播
broadcastx download https://x.com/i/broadcasts/1vAxRkBbDRzKl

# 从扫描结果批量下载
broadcastx download --from output/broadcasts.json

# 多并发下载
broadcastx download --from output/broadcasts.json -p 3

# 自定义输出目录
broadcastx download --from output/broadcasts.json -o ./videos

# 使用 Firefox cookies
broadcastx download --from output/broadcasts.json --browser firefox

# 显示 yt-dlp 详细信息
broadcastx download --from output/broadcasts.json -v
```

BroadcastX **自动修正手机旋转方向**：如果直播 HLS 流中包含方向元数据，视频将被重新编码以正确的方向播放。

### 监控直播

```bash
broadcastx monitor @username

# 单次检测（不循环）
broadcastx monitor @username --once

# 下载到自定义目录
broadcastx monitor @username -o ./my_videos

# 自定义检测间隔（秒）
broadcastx monitor @username --check-interval 1800 --live-interval 300

# 仅检测，不下载
broadcastx monitor @username --no-download
```

监控器循环运行：

1. **主页检查**（每隔 `check-interval` 秒，默认 30 分钟）—— 打开主页，查找直播卡片
2. **直播检测** —— 找到候选后判断是否为当前直播
3. **状态检查**（每隔 `live-interval` 秒，默认 5 分钟）—— 持续检查直到直播结束
4. **自动下载** —— 直播结束后自动下载回放

事件记录到 `output/monitor_events.json`。
### 抓取所有历史直播

```bash
broadcastx scrape @username

# 忽略已保存状态，重新开始
broadcastx scrape @username --fresh

# 添加延迟，显示详细输出
broadcastx scrape @username --delay 2.0 -v

# 直接提供登录凭证（跳过浏览器登录）
broadcastx scrape @username \
  --auth-token "your_auth_token" \
  --csrf-token "your_ct0" \
  --user-id "1234567890"
```

使用 GraphQL API 分页，支持游标恢复，可用于遍历全部历史记录。状态保存在本地，可在限速后继续。

## 输出目录结构

```
output/
├── broadcasts.json          # 扫描结果
├── monitor_events.json      # 监控事件日志
└── videos/
    ├── [title] [id].mp4     # 下载的直播视频
    ├── [id].rotation.jsonl  # 旋转方向时间线侧车文件
    └── ...
```

## 管线示例

```bash
# 扫描 + 下载所有找到的直播
broadcastx scan @username
broadcastx download --from output/broadcasts.json

# 监控并自动下载
broadcastx monitor @username -o ./videos

# 批量抓取 + 下载
broadcastx scrape @username
broadcastx download --from output/username_broadcasts.json
```

## 工作原理

### 扫描器
使用 Playwright 拦截 Twitter 的 GraphQL API 响应（`UserTweets` / `TweetDetail`）。比 DOM 解析更稳定，因为 JSON 响应结构变化频率远低于 HTML。

### 下载器
封装 `yt-dlp`（内置 `TwitterBroadcastIE` 提取器），并增加以下功能：
- **旋转侧车提取** —— 从 HLS 片段解析 timed-ID3 元数据
- **自动旋转修正** —— 通过 ffmpeg 重新编码视频，纠正方向

### 旋转侧车文件
JSONL 格式的侧车文件（`[id].rotation.jsonl`）每条记录对应一个 HLS 片段：
- `raw_rotation` —— Periscope 的原始传感器角度
- `rotation` —— 量化后的方向：0°、90°、180° 或 270°（带迟滞处理）
- `ntp` —— NTP 时间戳，用于时间线重建

## 开源协议

MIT
