# DS5Hub — 多手柄 USB/IP 一站式桥接中心

把多只蓝牙/USB DualSense 手柄变成标准 USB/IP 虚拟 USB 手柄的完整方案，
自动完成 HidHide 隐藏、USB/IP 服务、自动重连、日志、开机自启与 Web 管理。
替代原 ds5-usbip.exe 的手动三步流程。

## 运行

```bash
# 开发机（无驱动、无真实手柄）：模拟模式
python main.py --simulated --tray      # 正常启动（托盘）
python main.py --simulated --web-only  # 仅 Web 调试
python run_demo.py                     # 最小化演示（仅 usbip 服务）

# 目标机（真实手柄）—— M2 完成后启用
python main.py --simulated=false
```

- Web 管理面板：http://127.0.0.1:8080
- usbip 服务端口：每个手柄一个，基端口 3240（可配置）

## 测试

```bash
python tests/mock_usbip_client.py 3240   # 模拟 Linux usbip 客户端的端到端协议测试
python tests/test_web_api.py             # Web API 冒烟测试
python -m ds5hub.usbip_protocol          # 协议模块自测
```

## 架构

```
Web 管理界面 (FastAPI + 静态页, 8080)
        │
核心控制层: app.py (DS5HubApp)
  ├─ pad_manager.py   多手柄枚举/状态机 (PadSlot)
  ├─ usbip_server.py  每手柄一 TCP 服务 (协议: usbip 1.0)
  ├─ usbip_protocol.py 协议编解码 (纯逻辑)
  ├─ logger.py        文件日志 + 内存环形缓冲
  ├─ autostart.py     HKCU Run 开机自启
  └─ web_api.py       REST API
驱动链路(目标机): HidHide 隐藏真实手柄 + hidapi 读写 + usbipd-win vhci attach
```

## 目录

```
ds5hub/
  DESIGN.md           设计文档
  main.py             入口（托盘+Web+服务）
  run_demo.py         开发机演示
  ds5hub/             核心包
    config.py        配置管理
    logger.py        日志
    pad_manager.py   多手柄管理（含 SimulatedPad 桩）
    usbip_protocol.py usbip 1.0 协议编解码
    usbip_server.py   usbip 服务端（描述符/URB 处理）
    app.py            核心协调
    web_api.py        FastAPI 路由
    web/static/       前端页面
    autostart.py      开机自启
    hid_pad.py        (M2) 真实 hidapi 手柄适配
    hidhid_manager.py (M2) HidHide 自动化
  tests/
    mock_usbip_client.py 端到端协议测试
    test_web_api.py      Web API 测试
```

## 状态

- [x] M1：usbip 协议服务端（标准 usbip 1.0，兼容 Linux usbip/usbipd-win 客户端）
- [x] M1：模拟手柄桩 + 多手柄槽位 + Web 管理面板 + 日志 + 自启
- [x] M1：端到端协议测试（DEVLIST/IMPORT/枚举/HID 报告/OUT/UNLINK）
- [ ] M2：真实 hidapi 枚举 + HidHide 自动化 + 自动重连（开发机无驱动环境）
- [ ] M3：目标机安装器 + PyInstaller 打包 + 真实手柄联调