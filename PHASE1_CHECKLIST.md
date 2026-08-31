# DS5Hub 阶段一：蓝牙模拟有线链路验证清单（usbip-win2 方案）

> 生成时间：2026-08-31。目标：验证「蓝牙 DualSense → DS5Hub 服务端（模拟 USB DualSense）
> → usbip-win2 客户端 attach → 本机虚拟 USB DualSense」完整链路。
>
> **方案已纠偏**：弃用 cezanne/usbip-win（测试签名 + TESTSIGNING，需关 Secure Boot），
> 改用 **usbip-win2（vadimgrn/usbip-win2）**——WHLK/OSSign 签名的 vhci 驱动，
> **免测试模式、免关 Secure Boot**。

## 一、方案要点

| 项目 | 说明 |
|---|---|
| 客户端 | usbip-win2（`USBip-0.9.7.8-x64.exe`，已下载到 `redist/`） |
| vhci 驱动 | WHLK/OSSign 签名，安装器自动部署，免测试模式 |
| 服务端 | DS5Hub 自研（内置虚拟 DualSense 描述符 054c:0ce6） |
| 端口 | DS5Hub 基端口 3240（可配；若残留旧 usbipd-win 需先卸载以释放 3240） |
| usbip.exe | 4 个子命令：`attach` / `detach` / `list` / `port`（无 `install`，驱动由安装器装） |

## 二、安装 usbip-win2（管理员）

1. 双击 `redist/USBip-0.9.7.8-x64.exe`（Inno Setup 安装向导），或由 DS5Hub
   「一键环境部署」自动静默安装（`/VERYSILENT /NORESTART`）。
2. 安装后 `usbip.exe` 位于 `C:\Program Files\USBip\usbip.exe`。
3. 验证：`usbip.exe port`（vhci 驱动就绪则正常列出，无设备为空）。

## 三、端到端验证

1. 蓝牙连接 DualSense（或 USB 有线直连先验证）。
2. 启动 DS5Hub（基端口 3240）。
3. `usbip.exe list -r 127.0.0.1` → 应看到 DS5Hub 导出的 DualSense（busid）。
4. `usbip.exe attach -r 127.0.0.1 -b <busid>` → 本机出现虚拟 USB DualSense。
   > 注意：短参数 `-t` 在 attach 子命令里是 `--terse`，全局 `--tcp-port` 的短参数也是
   > `-t`（冲突）。自定义端口务必用长参数 `usbip.exe --tcp-port <port> attach …`。
5. 游戏 / `usbip.exe port` 验证虚拟设备。

## 四、真机联调前置状态（2026-08-31 更新）

- ✅ `report_descriptor()` 已替换为真实 DualSense USB 有线版 **273 字节**描述符
  （与 Linux 内核 hid-playstation.c / nondebug-dualsense 实测一致，非逆向 func197），
  自适应扳机/触觉反馈的 HID 字段声明已就位；`_hid_descriptor()` 长度已同步为 273，
  `compute_size_check` 索引 bug（hid[5:7]→hid[7:9]）已修复，协议握手自测通过。
- ⚠️ **报告「数据」字段重排未实现（真机联调首要验证点）**：蓝牙 DualSense 输入报告
  （hidapi 读到 report id 0x31/0x01，78/64 字节）→ USB 有线输入报告（64 字节，
  report id 0x01，字段序：摇杆×4/L2R2/序列号/hat+15按键/触摸板/52B 传感器）的重排
  逻辑尚未在 `hid_pad.py` 报告读取层实现；当前为原样透传，游戏端可能解析错乱。
- 需真机验证 HidHide 隐藏蓝牙手柄 → hidapi 透传 → usbip-win2 attach 完整链路，
  以及 HidHide class filter 写入后需重启生效。
