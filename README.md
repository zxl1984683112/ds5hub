# DS5Hub — 多手柄 USB/IP 一站式桥接中心

把多只蓝牙/USB DualSense 手柄变成标准 USB/IP 虚拟 USB 手柄的完整方案，
自动完成 HidHide 隐藏、USB/IP 服务、自动重连、日志、开机自启与 Web 管理。
替代原 ds5-usbip.exe 的手动三步流程。

## 运行

```bash
# 启动（真实手柄模式，hidapi 枚举；模拟模式配置已移除）
python main.py --tray                  # 托盘常驻启动
python main.py --web-only              # 仅 Web 调试（无手柄时面板显示空列表）
```

- Web 管理面板：http://127.0.0.1:8080
- usbip 服务端口：每个手柄一个，基端口 3240（可配置）

## 测试

```bash
python tests/mock_usbip_client.py 3240   # 模拟 Linux usbip 客户端的端到端协议测试
python tests/test_web_api.py             # Web API 冒烟测试
python test_final.py                     # 端到端集成测试（39 项）
python test_orchestrator.py              # 一键环境部署 + 自动 attach 测试（18 项，dry_run 安全）
python -m ds5hub.usbip_protocol          # 协议模块自测
python -m ds5hub.install_orchestrator    # 部署编排器自测
```

## 一键环境部署（M3）

Web 面板「组件检测」卡片内置 **🚀 一键环境部署**：自动安装官方
HidHide 驱动与 usbipd-win，并在完成后对每只 EXPOSED 手柄自动执行
本机 `usbip attach`（虚拟设备直插系统）。

```
PREPARING   获取官方 msi：内嵌 redist → 项目/redist → %LOCALAPPDATA%\DS5Hub\redist
            → GitHub Releases latest 下载（nefarius/HidHide、dorssel/usbipd-win）
INSTALLING  msiexec /qn /norestart 静默安装（Start-Process -Verb RunAs，仅 1 次 UAC）
VERIFYING   HidHide 服务 + usbipd 服务 + HidHideCLI/usbip.exe 就绪验证
POST        DS5Hub 自动加入 HidHide 白名单
DONE / NEEDS_REBOOT / FAILED（msiexec 3010 → NEEDS_REBOOT）
```

- 许可合规：捆绑/下载的均为原封官方 MSI（GPL 系开源组件），DS5Hub 不修改不链接
- 开发机零驱动：配置 `orchestrator.dry_run: true` 可完整模拟部署流程（18 项测试）
- 自动 attach：每手柄仅自动尝试一次；失败可通过 Web 面板或
  `POST /api/pads/{id}/attach` 手动重试（非管理员会触发 UAC）
- 相关配置：`orchestrator.dry_run` / `orchestrator.auto_attach` / `orchestrator.attach_interval`
- 相关 API：`GET /api/environment` · `POST /api/environment/deploy` · `POST /api/pads/{id}/attach`

## 一键卸载（M3）

Web 面板「设置 → 危险区」内置 **🗑️ 一键卸载**，与一键部署对等的完整反操作，
顺序经过安全设计（任何一步失败即中止，绝不出现"驱动已卸、隐藏仍在"死锁态）：

```
UNHIDING    解除所有 HidHide 隐藏（第一步必做——先卸驱动会让真实手柄从系统消失）
STOPPING    停止 DS5Hub 全部服务（usbip/attach/重连/托盘）
AUTOSTART   移除 HKCU 开机自启
COMPONENTS  可选：卸载官方驱动组件（默认关闭——HidHide 是通用过滤驱动，
            Steam 输入映射等第三方工具可能依赖；从注册表 Uninstall 键枚举
            官方 MSI ProductCode 后 msiexec /x {GUID} /qn 静默卸载）
CLEANUP     可选：清理 %APPDATA%\DS5Hub（配置/日志）与 msi 缓存
SELF_DELETE 延迟自删 exe（仅 PyInstaller frozen 模式；源码运行绝不自删）
DONE / NEEDS_REBOOT / FAILED
```

- 组件卸载为可选勾选项，需两层确认（弹窗警示第三方依赖风险）
- 非 MSI 安装器组件不会被静默重放，仅提示手动处理
- 开发机零驱动：`orchestrator.dry_run: true` 模拟全流程（16 项测试）
- 相关 API：`GET /api/uninstall` · `POST /api/uninstall`（body: `remove_components` / `remove_config`）

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
  run_demo.py         （已废弃占位：模拟模式已移除）
  ds5hub/             核心包
    config.py        配置管理
    logger.py        日志
    pad_manager.py   多手柄管理
    usbip_protocol.py usbip 1.0 协议编解码
    usbip_server.py   usbip 服务端（描述符/URB 处理）
    app.py            核心协调
    web_api.py        FastAPI 路由
    web/static/       前端页面
    autostart.py      开机自启
    hid_pad.py        (M2) 真实 hidapi 手柄适配
    hidhid_manager.py (M2) HidHide 自动化
    reconnect.py      (M2) 自动重连策略
    installer.py      (M3) 组件检测
    install_orchestrator.py (M3) 一键环境部署编排（下载/静默安装/验证/attach）
    uninstall_orchestrator.py (M3) 一键卸载编排（解除隐藏/停服/卸组件/自删）
  tests/
    mock_usbip_client.py 端到端协议测试
    test_web_api.py      Web API 测试
  test_final.py          端到端集成测试
  test_orchestrator.py   部署编排器测试
  test_uninstall.py      卸载编排器测试
```

## 状态

- [x] M1：usbip 协议服务端（标准 usbip 1.0，兼容 Linux usbip/usbipd-win 客户端）
- [x] M1：模拟手柄桩 + 多手柄槽位 + Web 管理面板 + 日志 + 自启
- [x] M1：端到端协议测试（DEVLIST/IMPORT/枚举/HID 报告/OUT/UNLINK）
- [x] M2：真实 hidapi 枚举 + HidHide 自动化 + 自动重连（开发机无驱动环境）
- [x] M3：一键环境部署编排器（官方 msi 下载/静默安装/验证/自动 attach，dry_run 可测）
- [ ] M3：目标机真实安装验证 + PyInstaller 打包 + 真实手柄联调