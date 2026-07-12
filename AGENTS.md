# stackchan-mcp (custom fork)

Forked from [kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp) for a single M5Stack StackChan device. Chinese-only documentation.

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Sync with upstream `kisaragi-mochi/stackchan-mcp` |
| `custom` | Device-specific customizations |

## Repository structure

```
  gateway/          Python MCP gateway
  firmware/         ESP32 firmware (xiaozhi-esp32 fork, stackchan board)
    main/boards/stackchan/  Board-specific code
    build/                  Build artifacts (gitignored)
  docs/
```

## Getting started

| Task | Where to look |
|---|---|
| Full setup guide | `GUIDE.md` |
| Build firmware | `docker run ... python ./scripts/release.py stackchan` |
| Flash firmware | `esptool.py ... write_flash 0x20000 build/xiaozhi.bin` |
| Private config | `firmware/sdkconfig.defaults.local` (gitignored) |
| Board details | `firmware/main/boards/stackchan/AGENTS.md` |

## License

MIT, with GPL-3.0 exception for SCServo_lib files. See `LICENSE`.
