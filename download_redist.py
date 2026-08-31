# -*- coding: utf-8 -*-
"""下载官方组件到 redist/（资产名已通过 expanded_assets 确认）。"""
import os
import shutil
import urllib.request
from pathlib import Path

PROXY = "http://127.0.0.1:7897"

FILES = [
    ("https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe",
     "HidHide_1.5.230_x64.exe"),
    ("https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.7.8/USBip-0.9.7.8-x64.exe",
     "USBip-0.9.7.8-x64.exe"),
]


def main():
    os.makedirs("redist", exist_ok=True)
    handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    opener = urllib.request.build_opener(handler)
    for url, name in FILES:
        dest = Path("redist") / name
        if dest.exists():
            print(f"已存在，跳过: {name}")
            continue
        print(f"下载: {name}")
        req = urllib.request.Request(url, headers={"User-Agent": "DS5Hub"})
        with opener.open(req, timeout=600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        print(f"  完成: {dest.stat().st_size // 1024 // 1024} MB")
    print("\nredist 目录内容:")
    for f in sorted(Path("redist").iterdir()):
        if f.is_file():
            print(f"  {f.name}  {f.stat().st_size // 1024 // 1024} MB")


if __name__ == "__main__":
    main()
