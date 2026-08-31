# -*- coding: utf-8 -*-
"""检查 USBip-x64.exe 安装程序类型 + 静默参数线索"""
data = open(r"D:\QwenpawWorkspace\project\ds5hub\redist\USBip-0.9.7.8-x64.exe", "rb").read()
print("size:", len(data))
for pat, name in [
    (b"Inno Setup", "InnoSetup"),
    (b"InnoSetup", "InnoSetup"),
    (b"Nullsoft", "NSIS"),
    (b"NSIS", "NSIS"),
    (b"WiX", "WiX"),
    (b"Windows Installer", "MSI(WiX)"),
    (b"Advanced Installer", "AdvancedInstaller"),
    (b"Bootstrapper", "Bootstrapper"),
]:
    if pat in data:
        print("FOUND:", name)
for kw in [b"/VERYSILENT", b"/SILENT", b"/qn", b"/quiet", b"/norestart", b"/S", b"--silent", b"--quiet", b"-q"]:
    idx = data.find(kw)
    if idx >= 0:
        print("silent kw:", kw, "at", hex(idx))
