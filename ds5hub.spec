# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（单文件 Windows exe，无控制台、托盘常驻）。

内嵌内容：
  - ds5hub/web/static        Web 管理界面静态资源
  - redist/                  官方组件安装包（HidHide_*.exe + USBip-*-x64.exe），
                             供"一键环境部署"在本机释放使用

使用方式：
  pyinstaller --clean --noconfirm ds5hub.spec
（产物在 dist/ds5hub.exe）
"""
import os
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ds5hub/web/static', 'ds5hub/web/static'),
        ('redist', 'redist'),
    ],
    hiddenimports=[
        'hid',             # cython-hidapi（import hid）
        'PIL',             # pystray 依赖 pillow
        'pystray',
        'uvicorn',
        'fastapi',
        'starlette',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'setuptools',
        'numpy',
        'pandas',
        'scipy',
        'matplotlib',
        'jinja2',
        'pytest',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,      # 单文件：二进制并入 EXE
    a.datas,         # 单文件：数据（含 redist）并入 EXE
    [],
    name='ds5hub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # 未预装 UPX；msi/exe 资产本身已压缩
    console=False,   # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,       # 如有图标，改传 "ds5hub.ico"
)
