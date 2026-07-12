# stackchan-mcp

M5Stack StackChan 通过 MCP 连接 Claude Code / OpenCode 的定制配置。

基于 [kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp)，针对自用设备做了定制。

## 硬件

M5Stack 官方 StackChan 套件：CoreS3 (ESP32-S3) + SCS0009 舵机 ×2 + GC0308 摄像头

## 快速开始

```bash
# 安装工具
uv tool install esptool
uv tool install stackchan-mcp

# 编译固件
cd firmware
docker run --rm --cpus=4 --ulimit nofile=65536:65536 \
  -v "$PWD":/project -w /project \
  espressif/idf:v5.5.2 python ./scripts/release.py stackchan

# 刷写（插 USB 到电脑，刷完拔掉改用充电头供电）
esptool.py --chip esp32s3 --port /dev/ttyACM0 -b 460800 \
  write_flash 0x0 build/merged-binary.bin

# 启动 Gateway
STACKCHAN_TOKEN=<token> VISION_HOST=<本机IP> stackchan-mcp
```

## MCP Client 配置

`~/.claude.json` 或 `~/.config/opencode/opencode.jsonc`：

```json
{
  "mcpServers": {
    "stackchan-mcp": {
      "type": "stdio",
      "command": "stackchan-mcp",
      "env": {
        "STACKCHAN_TOKEN": "<token>",
        "VISION_HOST": "<本机IP>"
      }
    }
  }
}
```

## 私有配置

`firmware/sdkconfig.defaults.local`（gitignored）：

```
CONFIG_DEFAULT_WEBSOCKET_URL="ws://<本机IP>:8765/"
CONFIG_DEFAULT_WEBSOCKET_TOKEN="<token>"
CONFIG_FORCE_DEFAULT_WEBSOCKET_URL=y
```

## 分支

| 分支 | 用途 |
|------|------|
| `main` | 同步上游 `kisaragi-mochi/stackchan-mcp` |
| `custom` | 本设备定制（当前） |

## 详细文档

见 [GUIDE.md](GUIDE.md) — 完整配置过程、踩坑记录、工具列表。

## License

上游仓库 MIT 协议，详见 [LICENSE](LICENSE)。
