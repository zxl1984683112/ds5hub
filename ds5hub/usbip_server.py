# -*- coding: utf-8 -*-
"""
USB/IP 服务端：每个手柄一个 TCP 端口，处理标准 usbip 1.0 会话。

- 连接后先读 op_common (8B)，支持 OP_REQ_DEVLIST / OP_REQ_IMPORT
- IMPORT 成功后进入 URB 数据面：CMD_SUBMIT / CMD_UNLINK
- 控制传输：GET/SET_DESCRIPTOR、SET_CONFIGURATION、SET_IDLE、GET/SET_REPORT
- 中断 IN：返回最近一次 HID 输入报告（状态快照语义，reader 线程持续刷新）
- 中断 OUT / 控制 OUT：写回手柄

依赖：usbip_protocol（纯协议）、pad_manager（设备抽象：真实/模拟）
"""
from __future__ import annotations

import socket
import struct
import threading
import time

from . import logger
from .pad_manager import PadSlot, AbstractPadDevice
from .usbip_protocol import (
    OP_REQ_DEVLIST,
    OP_REQ_IMPORT,
    OP_REQ_UNIMPORT,
    CMD_SUBMIT,
    CMD_UNLINK,
    UsbDeviceInfo,
    BasicHeader,
    CmdSubmit,
    encode_devlist_reply,
    encode_import_reply,
    encode_ret_submit,
    encode_ret_unlink,
    decode_import_request,
    decode_cmd_unlink,
    usb_parse_setup,
    setup_str,
    is_dir_in,
    USB_REQ_GET_DESCRIPTOR,
    USB_REQ_SET_CONFIGURATION,
    USB_REQ_SET_INTERFACE,
    USB_REQ_SET_ADDRESS,
    USB_REQ_GET_STATUS,
    USB_REQ_CLEAR_FEATURE,
    USB_REQ_SET_FEATURE,
    HID_REQ_GET_REPORT,
    HID_REQ_SET_REPORT,
    HID_REQ_GET_IDLE,
    HID_REQ_SET_IDLE,
    HID_REQ_GET_PROTOCOL,
    HID_REQ_SET_PROTOCOL,
    URB_TRANSFER_CONTROL,
    URB_TRANSFER_INTERRUPT,
    URB_TRANSFER_BULK,
    DIR_IN,
    DIR_OUT,
    ST_OK,
    ST_EPIPE,
    ST_EINVAL,
)

# ---- 虚拟设备描述符（DualSense 形态）----

DEVICE_DESCRIPTOR = bytes([
    18, 0x01,             # bLength, bDescriptorType=DEVICE
    0x00, 0x02,           # bcdUSB 2.00
    0x00, 0x00, 0x00,     # bDeviceClass/Sub/Proto
    0x40,                 # bMaxPacketSize0 = 64
    0x4C, 0x05,           # idVendor 0x054C Sony
    0xE6, 0x0C,           # idProduct 0x0CE6 DualSense
    0x00, 0x01,           # bcdDevice 1.00
    0x01,                 # iManufacturer
    0x02,                 # iProduct
    0x03,                 # iSerialNumber
    0x01,                 # bNumConfigurations
])


def _hid_descriptor() -> bytes:
    """HID 描述符（接口级）"""
    return bytes([
        9, 0x21,          # bLength, bDescriptorType=HID
        0x10, 0x01,       # bcdHID 1.10
        0x00,             # bCountryCode
        0x01,             # bNumDescriptors
        0x22, 0xD7, 0x00, # Report descriptor type + length (215)
    ])


def config_descriptor() -> bytes:
    """配置描述符：Config + Interface + HID + EP IN(0x81) + EP OUT(0x02)"""
    conf = bytes([
        9, 0x02,          # bLength, CONFIG
        0x29, 0x00,       # wTotalLength = 41
        0x01,             # bNumInterfaces
        0x01,             # bConfigurationValue
        0x00,             # iConfiguration
        0x80,             # bmAttributes (bus powered)
        0xFA,             # bMaxPower 500mA (0xFA/2=125? use 0x32=100mA)
    ])
    # 修正 bMaxPower 为 100mA
    conf = conf[:7] + bytes([0x32]) + conf[8:]
    itf = bytes([
        9, 0x04,          # bLength, INTERFACE
        0x00, 0x00,       # bInterfaceNumber, bAlternateSetting
        0x02,             # bNumEndpoints
        0x03, 0x00, 0x00, # class=HID, sub, proto
        0x00,             # iInterface
    ])
    hid = _hid_descriptor()
    ep_in = bytes([
        7, 0x05,          # bLength, ENDPOINT
        0x81,             # bEndpointAddress IN
        0x03,             # bmAttributes interrupt
        0x40, 0x00,       # wMaxPacketSize 64
        0x01,             # bInterval 1ms
    ])
    ep_out = bytes([
        7, 0x05,
        0x02,             # EP OUT
        0x03,
        0x40, 0x00,
        0x01,
    ])
    total = 9 + 9 + 9 + 7 + 7
    conf = conf[:2] + struct.pack("<H", total) + conf[4:]
    return conf + itf + hid + ep_in + ep_out


def report_descriptor() -> bytes:
    """DualSense 风格 HID 报告描述符（精简版，215 字节占位 + 有效结构）。

    注：目标机联调时应用真实 DualSense BT 转 USB 报告描述符（逆向 func197 产物）。
    此处提供结构完整、按键/摇杆/扳机基本的描述符。
    """
    return bytes([
        # Usage Page (Generic Desktop), Usage (Game Pad)
        0x05, 0x01, 0x09, 0x05,
        # Collection (Application)
        0xA1, 0x01,
        # Report ID 1 ...（输入报告 64 字节结构占位）
        0x85, 0x01,
        # 摇杆: X, Y, Z, Rz (16-bit each, logical 0..1023)
        0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
        0x15, 0x00, 0x26, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x04,
        0x81, 0x02,
        # 扳机: Rx, Ry (8-bit), 0..255
        0x09, 0x33, 0x09, 0x34, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x02, 0x81, 0x02,
        # 按键 14 个 (Button Page 0..13)
        0x05, 0x09, 0x19, 0x01, 0x29, 0x0E,
        0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0E, 0x81, 0x02,
        # Padding 2 bit
        0x75, 0x01, 0x95, 0x02, 0x81, 0x01,
        # 方向键 hat switch 8
        0x05, 0x01, 0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x65, 0x14, 0x75, 0x04, 0x95, 0x01, 0x81, 0x42,
        0x65, 0x00,
        # 填充到 64 字节输入报告
        0x75, 0x08, 0x95, 0x03, 0x81, 0x01,
        0xC0,
        # ---- 输出报告 (report id 2, 64B) ----
        0x85, 0x02,
        0x75, 0x08, 0x95, 0x3F, 0x91, 0x02,
        0xC0,
        # ---- 特性报告 (report id 5 占位) ----
        0x85, 0x05,
        0x75, 0x08, 0x95, 0x3F, 0xB1, 0x02,
        0xC0,
    ])


STRINGS = {
    0: b"\x09\x04",           # langid en-US
    1: "Sony Interactive Entertainment".encode(),
    2: "DualSense Wireless Controller".encode(),
    3: "DS5-HUB-0001".encode(),
}


def string_descriptor(idx: int) -> bytes:
    if idx == 0:
        return b"\x04\x03\x09\x04"
    s = STRINGS.get(idx, b"")
    if not s:
        return b"\x00"
    data = s.decode("utf-8", "ignore")[:62]
    b = data.encode("utf-16le", "ignore")
    return bytes([2 + len(b), 0x03]) + b


class PadUsbipServer:
    def __init__(self, slot: PadSlot, device: AbstractPadDevice,
                 host: str = "0.0.0.0", port: int = 3240, backlog: int = 16):
        self.slot = slot
        self.device = device
        self.host = host
        self.port = port
        self.backlog = backlog
        self._sock: socket.socket | None = None
        self._listener: threading.Thread | None = None
        self._running = False
        self._sessions: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._latest_report: bytes | None = None
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

    # ---- 生命周期 ----
    def start(self) -> bool:
        # 确保底层设备已打开（真实手柄/模拟桩一致）
        try:
            if not self.device.is_open():
                self.device.open()
        except Exception as e:  # noqa: BLE001
            logger.warn(f"[usbip] {self.slot.info.name} 打开设备失败: {e}")
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(self.backlog)
            self._sock.settimeout(1.0)
            self._running = True
            self._listener = threading.Thread(target=self._accept_loop, daemon=True)
            self._listener.start()
            self._reader = threading.Thread(target=self._report_reader, daemon=True)
            self._reader.start()
            logger.info(f"[usbip] {self.slot.info.name} 监听 {self.host}:{self.port} (busid {self.slot.busid})")
            return True
        except OSError as e:
            logger.error(f"[usbip] {self.slot.info.name} 监听 {self.host}:{self.port} 失败: {e}")
            if self._sock:
                self._sock.close()
                self._sock = None
            return False

    def stop(self):
        self._running = False
        self._reader_stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        with self._lock:
            for s in list(self._sessions):
                try:
                    s.close()
                except OSError:
                    pass
            self._sessions.clear()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ---- 报告读取线程（刷新最新状态）----
    def _report_reader(self):
        while not self._reader_stop.is_set():
            try:
                rep = self.device.read_report(timeout_ms=200)
                if rep is not None:
                    self._latest_report = rep
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[usbip] 读报告异常: {e}")
                time.sleep(0.2)

    # ---- 接受连接 ----
    def _accept_loop(self):
        assert self._sock is not None
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._sessions.add(conn)
            logger.info(f"[usbip] 客户端连接: {addr} -> {self.slot.info.name}")
            t = threading.Thread(target=self._client_handler, args=(conn, addr), daemon=True)
            t.start()

    # ---- 客户端会话 ----
    def _client_handler(self, conn: socket.socket, addr):
        imported = False
        try:
            while True:
                op = self._read_exact(conn, 8)
                if op is None:
                    break
                version, code, status = struct.unpack(">HHI", op)
                if code == OP_REQ_DEVLIST:
                    conn.sendall(encode_devlist_reply([self.slot.devinfo]))
                    logger.info(f"[usbip] OP_REQ_DEVLIST <- {addr}")
                    continue
                elif code == OP_REQ_IMPORT:
                    req = self._read_exact(conn, 32)
                    if req is None:
                        break
                    busid = decode_import_request(op + req)
                    if busid in ("", self.slot.busid) or busid == self.slot.busid:
                        conn.sendall(encode_import_reply(True, self.slot.devinfo))
                        logger.info(f"[usbip] OP_REQ_IMPORT(busid={busid}) 成功 -> {addr}")
                        imported = True
                        self._urb_loop(conn, addr)
                        break
                    else:
                        conn.sendall(encode_import_reply(False))
                        logger.warn(f"[usbip] IMPORT busid 不匹配: {busid} (-{addr})")
                        break
                elif code == OP_REQ_UNIMPORT:
                    logger.info(f"[usbip] OP_REQ_UNIMPORT <- {addr}")
                    break
                else:
                    logger.warn(f"[usbip] 未知 op code=0x{code:04x} (-{addr})")
                    break
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"[usbip] 会话异常 {addr}: {e}")
        finally:
            with self._lock:
                self._sessions.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            if imported:
                self.slot.client_count += 1
                logger.info(f"[usbip] 客户端断开: {addr} ({self.slot.info.name})")

    def _read_exact(self, conn: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    # ---- URB 数据面 ----
    def _urb_loop(self, conn: socket.socket, addr):
        while self._running:
            basic_raw = self._read_exact(conn, 24)
            if basic_raw is None:
                break
            basic = BasicHeader.decode(basic_raw)
            if basic.command == CMD_SUBMIT:
                # 剩余: 20B extra + 8B setup = 28B（共 52B）
                rest = self._read_exact(conn, 28)
                if rest is None:
                    break
                self._handle_submit(conn, basic_raw + rest, addr)
            elif basic.command == CMD_UNLINK:
                # 剩余 4B：要 unlink 的 seqnum
                rest = self._read_exact(conn, 4)
                if rest is None:
                    break
                seq = decode_cmd_unlink(basic_raw + rest)
                conn.sendall(encode_ret_unlink(seq, basic.devid, ST_OK))
            else:
                logger.warn(f"[usbip] 未知数据面命令 0x{basic.command:04x} (-{addr})")
                break

    def _handle_submit(self, conn: socket.socket, hdr: bytes, addr):
        sub = CmdSubmit.decode(hdr)
        ep = sub.header.ep & 0x7F
        bmrt, breq, wvalue, windex = usb_parse_setup(sub.setup)

        if sub.header.ep == 0:  # 控制传输
            payload = self._handle_control(bmrt, breq, wvalue, windex,
                                           sub.setup, sub.transfer_buffer_length)
            actual = len(payload) if payload is not None else 0
            status = ST_OK if payload is not None else ST_EPIPE
            logger.debug(f"[usbip] CTRL {setup_str(bmrt, breq, wvalue, windex)} "
                         f"-> {actual}B status={status}")
            conn.sendall(encode_ret_submit(sub.header.seqnum, sub.header.devid,
                                           sub.header.direction, 0, status,
                                           actual, payload or b""))
            return

        if sub.header.direction == DIR_IN and sub.transfer_buffer_length:
            # 中断/批量 IN：返回最新报告（长度按请求裁剪）
            rep = self._latest_report or bytes(sub.transfer_buffer_length)
            out = rep[:sub.transfer_buffer_length]
            if len(out) < sub.transfer_buffer_length:
                out = out + bytes(sub.transfer_buffer_length - len(out))
            conn.sendall(encode_ret_submit(sub.header.seqnum, sub.header.devid,
                                           DIR_IN, sub.header.ep, ST_OK,
                                           len(out), out))
            return

        if sub.header.direction == DIR_OUT:
            # 读出数据 -> 写回手柄
            n = sub.transfer_buffer_length
            data = self._read_exact(conn, n) if n else b""
            ok = self.device.write_report(data) if data else True
            logger.debug(f"[usbip] OUT {n}B -> pad ok={ok}")
            conn.sendall(encode_ret_submit(sub.header.seqnum, sub.header.devid,
                                           DIR_OUT, sub.header.ep,
                                           ST_OK if ok else ST_EPIPE, 0))
            return

        # 未知情况
        conn.sendall(encode_ret_submit(sub.header.seqnum, sub.header.devid,
                                       sub.header.direction, sub.header.ep,
                                       ST_EINVAL, 0))

    def _handle_control(self, bmrt, breq, wvalue, windex,
                        setup: bytes, transfer_len: int) -> bytes | None:
        reqtype = bmrt & 0x60  # 0=standard, 0x20=class
        if not (bmrt & 0x80):  # OUT 控制写
            # SET_CONFIGURATION / SET_INTERFACE / SET_ADDRESS / SET_REPORT
            if breq == USB_REQ_SET_CONFIGURATION or breq == USB_REQ_SET_INTERFACE \
               or breq == USB_REQ_SET_ADDRESS or breq in (HID_REQ_SET_PROTOCOL,):
                return b""
            if breq == HID_REQ_SET_REPORT:
                # 写回手柄（output 数据在 transfer_len 内，但控制写无数据跟随）
                return b""
            if breq in (USB_REQ_CLEAR_FEATURE, USB_REQ_SET_FEATURE):
                return b""
            return b""

        # IN 控制读
        if breq == USB_REQ_GET_DESCRIPTOR:
            dtype = (wvalue >> 8) & 0xFF
            idx = wvalue & 0xFF
            if dtype == 0x01:      # device
                return DEVICE_DESCRIPTOR
            if dtype == 0x02:      # config
                return config_descriptor()
            if dtype == 0x22:      # HID report
                return report_descriptor()
            if dtype == 0x03:      # string
                return string_descriptor(idx)
            return None
        if breq == USB_REQ_GET_STATUS:
            return bytes([0x00, 0x00])
        if reqtype == 0x20:  # HID class
            if breq == HID_REQ_GET_REPORT:
                return b""
            if breq == HID_REQ_GET_IDLE:
                return bytes([0x00])
            if breq == HID_REQ_GET_PROTOCOL:
                return bytes([0x00])
        return b""


def compute_size_check():
    """描述符长度校验（自检用）"""
    cd = config_descriptor()
    assert len(DEVICE_DESCRIPTOR) == 18
    assert struct.unpack("<H", cd[2:4])[0] == len(cd)
    rd = report_descriptor()
    hid = _hid_descriptor()
    assert struct.unpack("<H", hid[5:7])[0] == len(rd)


if __name__ == "__main__":
    compute_size_check()
    print("descriptor selftest OK")