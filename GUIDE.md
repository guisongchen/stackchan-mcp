# StackChan MCP 配置指南

M5Stack 官方 StackChan 套件（CoreS3 + SCS0009 + GC0308）连接 MCP Client 的实战配置记录。

## 环境信息

| 项目 | 值 |
|------|-----|
| 工作目录 | `/home/ccc/projects/stackChan` |
| 操作系统 | Ubuntu 24.04 / x86_64 |
| 本机局域网 IP | `192.168.0.100` |
| StackChan MAC | `44:1b:f6:**:**:**` |
| StackChan IP | DHCP 分配 |
| Wi-Fi | 2.4GHz |
| Gateway Token | 见 `sdkconfig.defaults.local` |
| 上游仓库 | [`kisaragi-mochi/stackchan-mcp`](https://github.com/kisaragi-mochi/stackchan-mcp) |
| Fork 仓库 | [`guisongchen/stackchan-mcp`](https://github.com/guisongchen/stackchan-mcp) |

## 架构

```
┌──────────┐   stdio MCP   ┌────────────┐   WebSocket   ┌───────────┐
│ MCP Client│ ←──────────→ │  Gateway   │ ←───────────→ │ StackChan │
│(Claude/OC)│              │ (Python)   │               │ (ESP32-S3)│
└──────────┘               │ :8765      │               └───────────┘
                           │ :8766 HTTP │ ←── JPEG ──── (camera)
                           │ :8767 /mcp │
                           └────────────┘
```

## 实际配置路径（最终采用）

原始计划（[plan.md](plan.md)）预期：官方固件 → captive portal 配网 → 完成。

实际路径更曲折，以下是最终可行方案：

### 1. 安装工具

```bash
uv tool install esptool         # 刷机
uv tool install stackchan-mcp   # Gateway
```

### 2. 定制编译固件（Docker）

**为什么需要定制编译**：官方预编译固件依赖 captive portal（`http://192.168.4.1`）配置 Wi-Fi，但该页面 JavaScript 加载异常（详见[踩坑记录](#踩坑记录)），且 ESP32-S3 连接电脑 USB 时无法正常启动 AP 模式。

**编译命令**：

```bash
cd firmware
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project \
  espressif/idf:v5.5.2 python ./scripts/release.py stackchan
```

产物在 `firmware/build/merged-binary.bin`。

**私有配置**（不提交 Git）放在 `firmware/sdkconfig.defaults.local`：

```
CONFIG_DEFAULT_WEBSOCKET_URL="ws://192.168.0.100:8765/"
CONFIG_DEFAULT_WEBSOCKET_TOKEN="<token>"
CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y
```

### 3. 刷写固件

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  write_flash 0x0 firmware/build/merged-binary.bin
```

**日常增量更新**（保留 NVS，不丢 Wi-Fi 配置）：
```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  write_flash 0x20000 firmware/build/xiaozhi.bin
```

### 4. 启动设备

**关键**：刷写后**必须断开电脑 USB，改用 USB 电源适配器供电**。电脑的 USB-Serial/JTAG 连接会阻止 ESP32-S3 正常启动（详见[踩坑记录](#踩坑记录)）。

### 5. 配置 MCP Client

**Claude Code** — `~/.claude.json`：
```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "stackchan-mcp",
      "env": {
        "STACKCHAN_TOKEN": "<token>",
        "VISION_HOST": "192.168.0.100"
      }
    }
  }
}
```

**OpenCode** — `~/.config/opencode/opencode.jsonc`：
```jsonc
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "stackchan-mcp",
      "env": {
        "STACKCHAN_TOKEN": "<token>",
        "VISION_HOST": "192.168.0.100"
      }
    }
  }
}
```

配置后重启 MCP Client 即可加载。

### 6. 验证

Gateway 启动后设备会自动连接（Wi-Fi 凭据已存 NVS）。确认连接：

```
ESP32 connecting: device=44:1b:f6:**:**:**
ESP32 initialized: protocol=2024-11-05 server=stackchan v2.2.6
Discovered 40 tools on ESP32
ESP32 ready: tools=40
```

## 踩坑记录

### 坑 1：USB-Serial/JTAG 阻止设备启动

**现象**：固件刷入后设备无任何反应（无 AP 热点、无串口输出、NVS 保持全 `0xFF` 未被写入）。

**原因**：M5Stack CoreS3 通过 USB 连接电脑时，USB-Serial/JTAG 接口会保持芯片在下载模式，阻止正常启动。

**解决**：刷完后拔掉 USB，改用普通充电头供电。

### 坑 2：Captive Portal 页面加载失败

**现象**：设备 AP 模式正常（`Xiaozhi-E1B1` 可见），连接后打开 `http://192.168.4.1` 但页面一直显示 loading，无法填写 Wi-Fi 信息。

**尝试过的方案**：
- JavaScript console 直接 fetch `/submit` API → 可行，但 Gateway 配置（Advanced tab）需要单独提交 `/advanced/submit`
- 电脑 Wi-Fi 连接设备 AP → 信号太弱，无法扫描到

**最终方案**：定制编译固件，将 Gateway URL 和 Token 通过 Kconfig 嵌入，Wi-Fi 凭据首次通过 JS console 写入 NVS 后持久保留。

### 坑 3：串口权限

**现象**：`esptool.py` 报错 `Permission denied: '/dev/ttyACM0'`。

**解决**：`pkexec chmod 666 /dev/ttyACM0`（每次重新插拔 USB 后需重新执行）。

### 坑 4：Gateway 启动后设备不立即连接

**现象**：Gateway 重启后设备需要 30-60 秒才重连。

**原因**：设备 WebSocket 断连后进入指数退避重试（5s → 最长 60s）。

**解决**：保持 Gateway 持久运行，避免频繁重启。

### 坑 5：compile 时 Docker 超时

**现象**：`docker run` 编译固件在 LVGL/emoji 阶段超时或 OOM。

**解决**：使用 `--cpus=4 --ulimit nofile=65536:65536` 参数。

## Git 分支策略

```
origin   → guisongchen/stackchan-mcp   (Fork)
upstream → kisaragi-mochi/stackchan-mcp (上游)
```

| 分支 | 用途 |
|------|------|
| `main` | 与 upstream/main 同步，保持干净 |
| `custom` | 定制修改，基于 main 分支 |

同步上游：
```bash
git checkout main && git pull upstream main
git checkout custom && git rebase main
git push origin custom
```

**私有配置（不提交）**：`firmware/sdkconfig.defaults.local`（Wi-Fi、Token 等），已在 `.gitignore` 中排除。

## 修改 Wi-Fi

设备 NVS 中已存储 Wi-Fi 凭据，正常运行不需要操作。如需更换：

1. 新 Wi-Fi 不可用 → 设备约 30 秒后退回 AP 模式
2. 手机连接 `Xiaozhi-*` 热点
3. 打开 `http://192.168.4.1`，在 Basic 页面填入新 Wi-Fi
4. Advanced 页面填入 Gateway URL 和 Token
5. 保存后设备自动重连

（如果 captive portal 页面仍然加载失败，用浏览器 JS console 执行 `fetch('/submit',...)` 和 `fetch('/advanced/submit',...)` 直接提交。）

## 可用 MCP 工具（44 个）

| 类别 | 工具 |
|------|------|
| 运动 | `move_head`, `get_head_angles`, `set_servo_torque`, `set_auto_torque_release` |
| 表情 | `set_avatar`, `set_blink`, `set_mouth`, `set_mouth_sequence` |
| 摄像 | `take_photo` |
| 音频 | `set_volume`, `say`(TTS), `listen`(STT) |
| LED | `set_led`, `set_all_leds`, `set_leds`, `clear_leds` |
| 状态 | `get_status`, `get_device_info`, `get_touch_state` |
| 配置 | `gateway_config_get/set`, `set_touch_sensor_enabled` |
| 扩展 | `stackchan_follow_pose_stream`, `beat_mode_*`, I2C 工具等 |

## 日常操作速查

```bash
# 编译固件
cd firmware
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project \
  espressif/idf:v5.5.2 python ./scripts/release.py stackchan

# 增量刷写（保留 NVS）
esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  write_flash 0x20000 build/xiaozhi.bin

# 完整刷写（清 NVS）
esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  write_flash 0x0 build/merged-binary.bin

# 串口权限
pkexec chmod 666 /dev/ttyACM0

# Gateway 前台运行
STACKCHAN_TOKEN=<token> VISION_HOST=192.168.0.100 stackchan-mcp

# Gateway 后台运行
nohup env STACKCHAN_TOKEN=<token> VISION_HOST=192.168.0.100 \
  stackchan-mcp serve --transport streamable-http \
  > /tmp/stackchan-gateway.log 2>&1 &
```
