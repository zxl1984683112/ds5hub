# DS5Hub — 多手柄 USB/IP 一站式桥接中心（设计文档）

目标：把蓝牙/USB 连接的 PS5 DualSense 手柄（多只）变成标准 USB/IP 虚拟 USB
手柄，自动完成 HidHide 隐藏、USB/IP 服务、自动重连、日志、开机自启与 Web 管理，
替代原 ds5-usbip.exe 的手动三步流程。

## 一、最终使用流程（用户在目标机上的体验）

1. 安装 DS5Hub（单 exe + 依赖组件，自动检测缺失并引导/静默安装 HidHide、usbip-win2）。
2. 首次启动："引导向导"检测 → 安装驱动组件 → 配对蓝牙手柄。
3. 常驻托盘，开机自启。Web 管理界面 http://127.0.0.1:8080（可配置、可局域网访问）。
4. Web 界面显示所有手柄（名称/VID/PID/连接方式/状态）。一键"连接"→ 该手柄经
   标准 usbip 协议暴露；HidHide 自动隐藏真实手柄防双重输入，白名单放行本程序。
5. 单机回环为主场景：本机 usbip-win2（usbip 客户端 + vhci 驱动）attach 本机
   服务端口，Windows 侧出现完整功能虚拟 DualSense（自适应扳机/触觉/灯效）；
   Linux usbip / 远程主机 attach 为扩展场景。
6. 手柄断开自动重连；客户端断线自动清理；全程日志可查。

## 二、组件清单

| 组件 | 说明 | 依赖 |
|---|---|---|
| pad_manager.py | 多手柄枚举/管理 | hidapi (可选) |
| hid_pad.py | 真实 hidapi 手柄适配（M2） | hidapi |
| usbip_protocol.py | usbip 1.0 协议编解码（纯逻辑） | 无 |
| usbip_server.py | 每手柄一个 TCP 服务端口，处理 URB | 协议模块 |
| hidhid_manager.py | HidHide 检测/注册表配置/安装引导 (M2) | 注册表 + 可选 CLI |
| reconnect.py | 自动重连策略（手柄/客户端两层）(M2) | 无 |
| installer.py | 目标机组件检测与安装引导 (M3) | 无 |
| autostart.py | 开机自启（HKCU Run） | 无 |
| logger.py | 日志（文件轮转 + 等级 + 内存环形缓存供 Web） | 无 |
| web_api.py | FastAPI 路由（状态/手柄/配置/日志/操作/HidHide）(M2+) | fastapi, uvicorn |
| web/static | 前端页面（原生 JS，无构建） | 无 |
| main.py | 入口：托盘 + 调度 + 服务 + 命令行工具 (M3) | pystray, pillow |
| config.py | JSON 配置管理（环境变量覆盖）(M2) | 无 |

## 三、USB/IP 协议实现要点（标准 usbip 1.0）

- 默认端口：3240（可配）；每手柄一个端口（3240+i）或单端口多 busid。
- OP_REQ_DEVLIST (0x8005) → OP_REP_DEVLIST：设备列表（busid、VID/PID、class）。
- OP_REQ_IMPORT (0x8003) → OP_REP_IMPORT (0x0003 成功) → 进入 URB 转发。
- SUBMIT / UNLINK：控制传输（get/set descriptor、set_configuration、set_idle、
  get/set_report）、中断传输（input 报告上报、output 报告写回）。
- 设备描述符：VID=0x054C (Sony) PID=0x0CE6 (DualSense)，class HID；
  HID 报告描述符：DualSense USB 报告模板（沿用 func197 逆向结论）。

## 四、多手柄模型

- 每个物理手柄 = 一个"PadSlot"，含状态机：DISCONNECTED → CONNECTING →
  READY → EXPOSED(SERVICE_RUNNING)
- 每个 READY 的槽位可独立：暴露 usbip 端口、attach、detach、隐藏（HidHide）
- HidHide 按设备实例路径隐藏，白名单添加本程序 exe 路径与 usbip-win2 客户端

## 五、自动重连（M2）

### 手柄层
- 指数退避重试：初始 delay -> max_delay（默认 3s→30s 封顶）
- 最大重试次数限制（0=无限），恢复后自动重新暴露
- 心跳检测空闲超时

### 客户端层
- TCP 断开后清理会话，端口继续监听等待新连接
- 会话保活：可选心跳

## 六、日志

- 文件：%LOCALAPPDATA%\DS5Hub\logs\ds5hub.log（RotatingFileHandler，10MB×5）
- 内存环形缓存（最近 2000 条）供 Web /logs 轮询查看
- 级别：DEBUG/INFO/WARN/ERROR；可 Web 调整

## 七、开机自启

- HKCU\Software\Microsoft\Windows\CurrentVersion\Run → DS5Hub = exe 路径（--tray）
- 启动后最小化到托盘，自动开始扫描手柄并等待连接，不弹窗打扰

## 八、部署（M3）

- PyInstaller --onefile --noconsole，产物 ds5hub.exe
- 组件包（HidHide，usbip-win2）以资源形式内置，首次运行引导安装
- 打包命令：`pyinstaller --clean --noconfirm ds5hub.spec`

## 九、开发机（无驱动）测试策略

- 模拟配置已移除（SimulatedPad / --simulated / demo.pad_count 均已删除）：
  手柄一律通过 hidapi 真实枚举，无手柄时 pads 为空列表，mode 恒为 "real"
- 开发机测试通过 Web API 错误路径（未知 pad_id）与 usbip 协议脚本
  （mock_usbip_client.py 纯协议层）覆盖，不伪造手柄设备
- HidHide/驱动安装逻辑只做"检测+引导"路径测试，真实安装留目标机

## 十、里程碑

### M1 ✅（已完成）
- 项目骨架 + usbip 协议模块 + 模拟手柄 + Web 管理骨架 + 日志 + 自启
- 已通过端到端验证：DEVLIST/IMPORT/URB 枚举/HID 报告/OUT/UNLINK
- Web API 冒烟测试通过

### M2 ✅（进行中/基本完成）
- ✅ 真实 hidapi 枚举（USB/蓝牙双模接入）
- ✅ HidHide 自动化检测与设备隐藏引导
- ✅ 自动重连策略（指数退避，手柄+客户端两层）
- ✅ 配置系统支持环境变量覆盖
- ⏳ 真实硬件联调（需待目标机环境）

### M3 📋（进行中）
- ✅ 组件检测器（HidHide/usbip-win2/Python 环境）
- ✅ PyInstaller 打包配置 (ds5hub.spec，单文件 + 内嵌 redist 驱动)
- ✅ 命令行工具（--detect/--pack）
- ⏳ 完整 URB 转发实测
- ⏳ 用户文档完善
- ⏳ 图标嵌入与签名

## 十一、命令行用法

```bash
# 正常启动（模拟模式）
python main.py

# 真实手柄模式
python main.py --real

# 仅组件检测
python main.py --detect

# 生成安装引导脚本
python main.py --pack

# 仅 Web 服务
python main.py --web-only

# 托盘后台模式
python main.py --tray

# 自定义配置文件
python main.py --config D:\path\to\config.json
```

## 十二、环境变量

```bash
DS5HUB_WEB_PORT=8080          # Web 端口
DS5HUB_USBIP_BASE_PORT=3240   # USB/IP 基础端口
DS5HUB_LOG_LEVEL=INFO         # 日志级别
DS5HUB_RECONNECT_ENABLED=true # 启用自动重连
```
