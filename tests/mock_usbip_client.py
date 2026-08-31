# -*- coding: utf-8 -*-
"""
模拟 Linux usbip 客户端，端到端测试 DS5Hub 服务端：
1) OP_REQ_DEVLIST -> REP_DEVLIST
2) OP_REQ_IMPORT -> REP_IMPORT
3) URB 枚举：GET_DESCRIPTOR(device/config/report/string)
4) 周期性 interrupt IN 提交，索取输入报告
5) interrupt OUT 写回
无需驱动，纯 socket 测试。
"""
from __future__ import annotations

import socket
import struct
import sys
import time

sys.path.insert(0, r"D:\QwenpawWorkspace\project\ds5hub")
from ds5hub.usbip_protocol import (  # noqa: E402
    BasicHeader, CMD_SUBMIT, OP_REQ_DEVLIST, OP_REQ_IMPORT,
    encode_devlist_reply, decode_devlist_reply, encode_import_reply,
    decode_import_request, USB_REQ_GET_DESCRIPTOR, DIR_IN, DIR_OUT,
    setup_str, usb_parse_setup,
)


def build_op(code: int) -> bytes:
    return struct.pack(">HHI", 0x0111, code, 0)


def recv_exact(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接关闭")
        buf.extend(chunk)
    return bytes(buf)


def build_submit(seq: int, direction: int, ep: int, tlen: int,
                 setup: bytes | None = None, payload: bytes = b"") -> bytes:
    basic = BasicHeader(CMD_SUBMIT, seq, 0xABCD1234, direction, ep, 0).encode()
    extra = struct.pack(">IIIII", 0, tlen, 0, 1, 0)
    frame = basic + extra + (setup or bytes(8))
    if payload:
        frame += payload
    return frame


def main(host: str = "127.0.0.1", port: int = 3240):
    print(f"=== 连接 DS5Hub usbip 服务 {host}:{port} ===")
    s = socket.create_connection((host, port), timeout=5)

    # 1) DEVLIST（完整读取，复用同一连接）
    print("--- OP_REQ_DEVLIST ---")
    s.sendall(build_op(OP_REQ_DEVLIST))
    hdr = recv_exact(s, 12)
    ver, code, status, ndev = struct.unpack(">HHII", hdr)
    assert code == 0x0005, f"期望 OP_REP_DEVLIST, 得到 0x{code:04x}"
    print(f"   版本 0x{ver:04x} status={status} ndev={ndev}")
    reply_body = b""
    for i in range(ndev):
        dev_data = recv_exact(s, 312)
        reply_body += dev_data
        nif = dev_data[310]
        for j in range(nif):
            reply_body += recv_exact(s, 4)
    status, dev_list = decode_devlist_reply(hdr + reply_body)
    for d in dev_list:
        print(f"   device: busid={d.busid} vid=0x{d.id_vendor:04x} pid=0x{d.id_product:04x} "
              f"ifaces={d.interfaces} path={d.path}")

    # 2) IMPORT
    print("--- OP_REQ_IMPORT ---")
    busid = dev_list[0].busid
    s.sendall(build_op(OP_REQ_IMPORT) + busid.encode().ljust(32, b"\x00")[:32])
    ihdr = recv_exact(s, 8)
    print(f"   import hdr hex: {ihdr.hex()}")
    iver, icode, istatus = struct.unpack(">HHI", ihdr)
    assert icode == 0x0003
    print(f"   import status=0x{istatus:04x} ({'OK' if istatus == 0 else 'FAIL'})")
    if istatus != 0:
        print("   IMPORT 失败，退出")
        return 1
    idev_data = recv_exact(s, 312)
    from ds5hub.usbip_protocol import UsbDeviceInfo
    idev = UsbDeviceInfo.decode(idev_data, allow_interfaces=False)
    print(f"   imported: {idev.busid} vid=0x{idev.id_vendor:04x} pid=0x{idev.id_product:04x}")

    # 3) URB 枚举
    print("--- URB 枚举 ---")
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    # GET_DESCRIPTOR device
    s.sendall(build_submit(next_seq(), DIR_IN, 0, 18,
                           bytes([0x80, USB_REQ_GET_DESCRIPTOR, 0x00, 0x01, 0x00, 0x00, 0x12, 0x00])))
    rhdr = recv_exact(s, 36)
    rcmd, rseq, rdevid, rdir, rep, rstatus = struct.unpack_from(">IIIIII", rhdr)
    actual = struct.unpack_from(">I", rhdr, 32)[0]
    payload = recv_exact(s, actual)
    print(f"   DEVICE desc: seq={rseq} status={rstatus} len={actual}")
    assert payload[:2] == bytes([18, 0x01])
    assert payload[8:10] == bytes([0x4C, 0x05])  # vid Sony

    # GET_DESCRIPTOR config
    s.sendall(build_submit(next_seq(), DIR_IN, 0, 64,
                           bytes([0x80, USB_REQ_GET_DESCRIPTOR, 0x00, 0x02, 0x00, 0x00, 0x40, 0x00])))
    rhdr = recv_exact(s, 36)
    actual = struct.unpack_from(">I", rhdr, 32)[0]
    payload = recv_exact(s, actual)
    total_len = struct.unpack("<H", payload[2:4])[0]
    print(f"   CONFIG desc: len={actual} total={total_len} (端点: IN/OUT)")
    assert total_len == actual

    # GET_DESCRIPTOR HID report
    s.sendall(build_submit(next_seq(), DIR_IN, 0, 215,
                           bytes([0x80, USB_REQ_GET_DESCRIPTOR, 0x00, 0x22, 0x00, 0x00, 0xD7, 0x00])))
    rhdr = recv_exact(s, 36)
    actual = struct.unpack_from(">I", rhdr, 32)[0]
    payload = recv_exact(s, actual)
    print(f"   HID report desc: len={actual}, 首字节=0x{payload[0]:02x} (05=UsagePage)")

    # GET_DESCRIPTOR string (product)
    s.sendall(build_submit(next_seq(), DIR_IN, 0, 64,
                           bytes([0x80, USB_REQ_GET_DESCRIPTOR, 0x02, 0x03, 0x09, 0x04, 0x40, 0x00])))
    rhdr = recv_exact(s, 36)
    actual = struct.unpack_from(">I", rhdr, 32)[0]
    payload = recv_exact(s, actual)
    try:
        sstr = payload[2:].decode("utf-16le", "ignore")
        print(f"   STRING(product): {sstr!r}")
    except Exception:
        print("   STRING: 解析失败")

    # 4) 周期性 interrupt IN（假装 HID 轮询）
    print("--- interrupt IN 轮询 x3 ---")
    for _ in range(3):
        s.sendall(build_submit(next_seq(), DIR_IN, 1, 64))
        rhdr = recv_exact(s, 36)
        actual = struct.unpack_from(">I", rhdr, 32)[0]
        payload = recv_exact(s, actual)
        print(f"   IN报告: seq={struct.unpack_from('>I', rhdr, 4)[0]} len={actual} "
              f"head={payload[:4].hex()}")

    # 5) interrupt OUT 写回
    print("--- interrupt OUT (写回手柄) ---")
    out_payload = bytearray(64)
    out_payload[0] = 0x02  # 输出报告 id
    s.sendall(build_submit(next_seq(), DIR_OUT, 2, 64, payload=bytes(out_payload)))
    rhdr = recv_exact(s, 36)
    print(f"   OUT status={struct.unpack_from('>I', rhdr, 20)[0]} (0=OK)")

    # 6) UNLINK 测试（发送一个不存在的 seqnum）
    print("--- CMD_UNLINK ---")
    unlink = BasicHeader(0x0002, 0x9999, 0xABCD1234, 0, 0, 0).encode() + struct.pack(">I", 0x9999)
    s.sendall(unlink)
    rhdr = recv_exact(s, 28)
    print(f"   UNLINK reply code=0x{struct.unpack_from('>I', rhdr, 0)[0]:04x}")

    s.close()
    print("\n=== 端到端测试通过 ===")
    return 0


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3240
    sys.exit(main(port=port))