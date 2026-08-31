# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置。

使用方式：
  pyinstaller --onefile --noconsole --name ds5hub build.spec
  
或带图标：
  pyinstaller --onefile --noconsole --name ds5hub --icon=ds5hub.ico build.spec
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
    ],
    hiddenimports=[
        'hidapi',          # hidapi 可能不存在，需要隐藏导入
        'hidapi.hidapi',
        'hidapi.libusb1',
        'hidapi.windows',
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
    [],
    exclude_binaries=True,
    name='ds5hub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,       # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,           # 如果有图标，传 "--icon=xxx"
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ds5hub',
)
