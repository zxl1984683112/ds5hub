# -*- coding: utf-8 -*-
"""
USB/IP 1.0 协议编解码（纯逻辑，无驱动依赖）。

字节布局严格遵循 Linux 内核 include/uapi/linux/usbip.h：

  usbip_header_basic     6 x be32  = 24B: command, seqnum, devid,
                                        direction, ep, status
  usbip_header_cmd_submit basic + be32*5 + setup[8] = 52B
  usbip_header_ret_submit basic + be32*3 = 36B (+ payload)
  usbip_header_cmd_unlink basic + be32 = 28B
  usbip_header_ret_unlink basic + be32 = 28B

  op_common               be16 version + be16 code + be32 status = 8B
  op_devlist_reply        op_common + be32 ndev + devices + interfaces
  usbip_usb_device        path[256] busid[32] be32*3 be16*3 u8*6 = 312B
  usbip_usb_interface     u8*4 = 4B
  op_import_request       op_common + busid[32] = 40B
  op_import_reply         成功: op_common + usbip_usb_device = 320B

命令码 (16-bit, 放 op_common.code；header_basic.command 为 32-bit)：
  OP_REQ_DEVLIST=0x8005  OP_REP_DEVLIST=0x0005
  OP_REQ_IMPORT =0x8003  OP_REP_IMPORT =0x0003
  OP_REQ_UNIMPORT=0x8006 OP_REP_UNIMPORT=0x0006
  USBIP_CMD_SUBMIT=0x0001 USBIP_RET_SUBMIT=0x0003
  USBIP_CMD_UNLINK=0x0002 USBIP_RET_UNLINK=0x0004
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

USBIP_VERSION = 0x0111

# ---- 命令码 ----
OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003
OP_REQ_UNIMPORT = 0x8006
OP_REP_UNIMPORT = 0x0006

CMD_SUBMIT = 0x0001
RET_SUBMIT = 0x0003
CMD_UNLINK = 0x0002
RET_UNLINK = 0x0004

# ---- 状态码 ----
ST_OK = 0
ST_EPIPE = 0xFFFF          # usbip 用 16-bit status; -EPIPE 的补码表示
ST_ENOENT = 0xFFFE
ST_EINVAL = 0xFFEA

# URB 方向
DIR_OUT = 0
DIR_IN = 1

# USB 标准请求
USB_REQ_GET_STATUS = 0x00
USB_REQ_CLEAR_FEATURE = 0x01
USB_REQ_SET_FEATURE = 0x03
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_GET_DESCRIPTOR = 0x06
USB_REQ_SET_DESCRIPTOR = 0x07
USB_REQ_GET_CONFIGURATION = 0x08
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_GET_INTERFACE = 0x0A
USB_REQ_SET_INTERFACE = 0x0B
USB_REQ_SYNCH_FRAME = 0x0C
# HID 类请求
HID_REQ_GET_REPORT = 0x01
HID_REQ_GET_IDLE = 0x02
HID_REQ_GET_PROTOCOL = 0x03
HID_REQ_SET_REPORT = 0x09
HID_REQ_SET_IDLE = 0x0A
HID_REQ_SET_PROTOCOL = 0x0B

# URB 传输类型
URB_TRANSFER_CONTROL = 0
URB_TRANSFER_ISOCHRONOUS = 1
URB_TRANSFER_BULK = 2
URB_TRANSFER_INTERRUPT = 3

_BASIC_FMT = ">IIIIII"          # 24B

# ---- 基本头 ----


@dataclass
class BasicHeader:
    command: int
    seqnum: int
    devid: int
    direction: int
    ep: int
    status: int

    def encode(self) -> bytes:
        return struct.pack(_BASIC_FMT, self.command, self.seqnum,
                           self.devid, self.direction, self.ep, self.status)

    @classmethod
    def decode(cls, data: bytes) -> "BasicHeader":
        return cls(*struct.unpack(_BASIC_FMT, data[:24]))

    def __len__(self) -> int:
        return 24


# ---- 设备列表 ----


@dataclass
class UsbDeviceInfo:
    path: str = "/sys/devices/platform/ds5hub/usb1/1-1"
    busid: str = "1-1"
    busnum: int = 1
    devnum: int = 1
    speed: int = 3                 # 3 = USB 2.0 high speed
    id_vendor: int = 0x054C
    id_product: int = 0x0CE6
    bcd_device: int = 0x0100
    b_device_class: int = 0
    b_device_subclass: int = 0
    b_device_protocol: int = 0
    b_configuration_value: int = 1
    b_num_configurations: int = 1
    b_num_interfaces: int = 1
    interfaces: list = field(default_factory=list)  # [(class,sub,proto,pad), ...]

    def encode(self) -> bytes:
        """固定 312 字节（不含接口结构，接口仅用于 DEVLIST 回复）。"""
        path_b = self.path.encode("utf-8", "ignore")[:255] + b"\x00"
        path_b = path_b.ljust(256, b"\x00")
        busid_b = self.busid.encode("utf-8", "ignore")[:31] + b"\x00"
        busid_b = busid_b.ljust(32, b"\x00")
        out = bytearray()
        out += struct.pack(">256s32sIII", path_b, busid_b,
                           self.busnum, self.devnum, self.speed)
        out += struct.pack(">HHH", self.id_vendor, self.id_product, self.bcd_device)
        out += struct.pack(">BBBBBB", self.b_device_class, self.b_device_subclass,
                           self.b_device_protocol, self.b_configuration_value,
                           self.b_num_configurations, self.b_num_interfaces)
        return bytes(out)

    def encode_interfaces(self) -> bytes:
        out = bytearray()
        for itf in self.interfaces:
            out += struct.pack(">BBBB", itf[0], itf[1], itf[2], itf[3])
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes, allow_interfaces: bool = True) -> "UsbDeviceInfo":
        path_b = data[:256]
        busid_b = data[256:288]
        busnum, devnum, speed = struct.unpack_from(">III", data, 288)
        vid, pid, bcd = struct.unpack_from(">HHH", data, 300)
        (bdc, bdsc, bdpc, bcfg, bncfg, bnif) = struct.unpack_from(">BBBBBB", data, 306)
        interfaces = []
        off = 312
        if allow_interfaces:
            for _ in range(bnif):
                interfaces.append(tuple(struct.unpack_from(">BBBB", data, off)))
                off += 4
        return cls(
            path=path_b.split(b"\x00", 1)[0].decode("utf-8", "ignore"),
            busid=busid_b.split(b"\x00", 1)[0].decode("utf-8", "ignore"),
            busnum=busnum, devnum=devnum, speed=speed,
            id_vendor=vid, id_product=pid, bcd_device=bcd,
            b_device_class=bdc, b_device_subclass=bdsc, b_device_protocol=bdpc,
            b_configuration_value=bcfg, b_num_configurations=bncfg,
            b_num_interfaces=bnif, interfaces=interfaces)


def encode_devlist_reply(devices: list[UsbDeviceInfo]) -> bytes:
    out = bytearray()
    out += struct.pack(">HHII", USBIP_VERSION, OP_REP_DEVLIST, 0, len(devices))
    for d in devices:
        out += d.encode()
        out += d.encode_interfaces()
    return bytes(out)


def decode_devlist_reply(data: bytes) -> tuple[int, list[UsbDeviceInfo]]:
    ver, code, status, ndev = struct.unpack_from(">HHII", data, 0)
    devices = []
    off = 12
    for _ in range(ndev):
        dev = UsbDeviceInfo.decode(data[off:])
        devices.append(dev)
        off += 312 + 4 * dev.b_num_interfaces
    return status, devices


# ---- Import ----


def decode_import_request(data: bytes) -> str:
    """从 ≥40B 数据中解析 busid。"""
    try:
        busid_b = data[8:8 + 32]
        return busid_b.split(b"\x00", 1)[0].decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def encode_import_reply(ok: bool, dev: UsbDeviceInfo | None = None) -> bytes:
    if ok and dev is not None:
        return struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, ST_OK) + dev.encode()
    # 失败：status = 0xFFFF (-EPIPE)，不含 device 结构（客户端按 status 判断）
    return struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, ST_EPIPE)


def _selftest() -> None:
    dev = UsbDeviceInfo(busid="1-1",
                        path="/sys/devices/platform/ds5hub/usb1/1-1",
                        id_vendor=0x054C, id_product=0x0CE6,
                        interfaces=[(3, 0, 0, 0)])
    reply = encode_devlist_reply([dev])
    assert len(reply) == 12 + 312 + 4
    status, devs = decode_devlist_reply(reply)
    assert status == 0 and len(devs) == 1 and devs[0].busid == "1-1"
    assert devs[0].id_vendor == 0x054C and devs[0].interfaces == [(3, 0, 0, 0)]

    ok = encode_import_reply(True, dev)
    print("DEBUG ok len =", len(ok), "expect", 8 + 312)
    assert len(ok) == 8 + 312


# ---- 数据面 ----


@dataclass
class CmdSubmit:
    header: BasicHeader
    transfer_flags: int
    transfer_buffer_length: int
    start_frame: int
    number_of_packets: int
    interval: int
    setup: bytes

    @classmethod
    def decode(cls, data: bytes) -> "CmdSubmit":
        h = BasicHeader.decode(data)
        tf, tlen, sf, npack, interval = struct.unpack_from(">IIIII", data, 24)
        setup = data[44:52]
        return cls(h, tf, tlen, sf, npack, interval, setup)


def encode_ret_submit(seqnum: int, devid: int, direction: int, ep: int,
                      status: int, actual_length: int = 0,
                      payload: bytes = b"") -> bytes:
    basic = BasicHeader(RET_SUBMIT, seqnum, devid, direction, ep, status).encode()
    extra = struct.pack(">III", 0, 1, actual_length)   # error_count, number_of_packets, actual_length
    return basic + extra + payload


def decode_cmd_unlink(data: bytes) -> int:
    """返回要 unlink 的 seqnum（第 24 字节处 be32）。"""
    return struct.unpack_from(">I", data, 24)[0]


def encode_ret_unlink(seqnum: int, devid: int, status: int) -> bytes:
    basic = BasicHeader(RET_UNLINK, seqnum, devid, 0, 0, status).encode()
    return basic + struct.pack(">I", status)


# ---- setup 解析辅助 ----


def usb_parse_setup(setup: bytes) -> tuple[int, int, int, int]:
    """8 字节 setup -> (bmRequestType, bRequest, wValue, wIndex)"""
    bmrt = setup[0]
    breq = setup[1]
    wvalue = struct.unpack_from("<H", setup, 2)[0]
    windex = struct.unpack_from("<H", setup, 4)[0]
    return bmrt, breq, wvalue, windex


def setup_str(bmrt, breq, wvalue, windex) -> str:
    return (f"bmRT=0x{bmrt:02x} bReq=0x{breq:02x} "
            f"wVal=0x{wvalue:04x} wIdx=0x{windex:04x}")


def is_dir_in(bmrt: int) -> bool:
    return bool(bmrt & 0x80)


# ---- 自测 ----


def _selftest() -> None:
    dev = UsbDeviceInfo(busid="1-1",
                        path="/sys/devices/platform/ds5hub/usb1/1-1",
                        id_vendor=0x054C, id_product=0x0CE6,
                        interfaces=[(3, 0, 0, 0)])
    reply = encode_devlist_reply([dev])
    assert len(reply) == 12 + 312 + 4
    status, devs = decode_devlist_reply(reply)
    assert status == 0 and len(devs) == 1 and devs[0].busid == "1-1"
    assert devs[0].id_vendor == 0x054C and devs[0].interfaces == [(3, 0, 0, 0)]

    ok = encode_import_reply(True, dev)
    assert len(ok) == 8 + 312
    fail = encode_import_reply(False)
    assert len(fail) == 8

    # 构造一个 CMD_SUBMIT 帧 (GET_DESCRIPTOR device)，验证解码
    basic = BasicHeader(CMD_SUBMIT, 0x1234, 0xABCD, DIR_IN, 0, 0).encode()
    setup = bytes([0x80, USB_REQ_GET_DESCRIPTOR, 0x01, 0x00, 0x00, 0x00, 0x12, 0x00])
    frame = basic + struct.pack(">IIIII", 0, 18, 0, 0, 0) + setup
    sub = CmdSubmit.decode(frame)
    assert sub.header.seqnum == 0x1234 and sub.transfer_buffer_length == 18
    bmrt, breq, wvalue, windex = usb_parse_setup(sub.setup)
    assert bmrt == 0x80 and breq == USB_REQ_GET_DESCRIPTOR and wvalue == 0x0001

    ret = encode_ret_submit(0x1234, 0xABCD, DIR_IN, 0, 0, 18, bytes(18))
    assert len(ret) == 36 + 18

    print("usbip_protocol selftest: ALL OK")


if __name__ == "__main__":
    _selftest()