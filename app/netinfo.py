# -*- coding: utf-8 -*-
"""来源地址辅助：本地邻居表查询（IP <-> MAC），用于配对 IPv4 / IPv6。

背景：一次 TCP 连接只携带一个地址族。为了在「来源」列同时给出该客户端的
另一个地址族（默认显示 IPv4、鼠标悬停显示 IPv6），这里通过本机邻居表按 MAC
关联同一台主机在同一网段内的 IPv4 与 IPv6 地址。

仅使用本机 API（Windows GetIpNetTable2），耗时 <1ms；任何失败均静默降级。
"""
import ipaddress
import threading
import time

_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "data": None}
_TTL = 5.0

try:
    import ctypes
    _OK = True
except Exception:
    _OK = False


if _OK:
    class _SOCKADDR_IN(ctypes.Structure):
        _fields_ = [("sin_family", ctypes.c_ushort),
                    ("sin_port", ctypes.c_ushort),
                    ("sin_addr", ctypes.c_ubyte * 4),
                    ("sin_zero", ctypes.c_ubyte * 8)]

    class _SOCKADDR_IN6(ctypes.Structure):
        _fields_ = [("sin6_family", ctypes.c_ushort),
                    ("sin6_port", ctypes.c_ushort),
                    ("sin6_flowinfo", ctypes.c_ulong),
                    ("sin6_addr", ctypes.c_ubyte * 16),
                    ("sin6_scope_id", ctypes.c_ulong)]

    class _SOCKADDR_INET(ctypes.Union):
        _fields_ = [("ipv4", _SOCKADDR_IN), ("ipv6", _SOCKADDR_IN6)]

    class _MIB_IPNET_ROW2(ctypes.Structure):
        _fields_ = [("Address", _SOCKADDR_INET),
                    ("InterfaceIndex", ctypes.c_ulong),
                    ("InterfaceLuid", ctypes.c_ulonglong),
                    ("PhysicalAddress", ctypes.c_ubyte * 32),
                    ("PhysicalAddressLength", ctypes.c_ulong),
                    ("State", ctypes.c_int),
                    ("ReachabilityTime", ctypes.c_ulonglong)]

    class _MIB_IPNET_TABLE2(ctypes.Structure):
        _fields_ = [("NumEntries", ctypes.c_ulong),
                    ("Table", _MIB_IPNET_ROW2 * 1)]


def norm(ip):
    """规范化 IP 字符串（压缩形式、小写），非法返回空串。"""
    try:
        return ipaddress.ip_address(str(ip).strip()).compressed.lower()
    except Exception:
        return ""


def _read(family):
    """读取指定族的邻居表，返回 [(ip, mac)]；mac 为无分隔大写十六进制。"""
    if not _OK:
        return []
    out = []
    try:
        api = ctypes.WinDLL("iphlpapi.dll")
        tbl = ctypes.POINTER(_MIB_IPNET_TABLE2)()
        if api.GetIpNetTable2(family, ctypes.byref(tbl)) != 0:
            return []
        try:
            n = int(tbl.contents.NumEntries or 0)
            if n <= 0:
                return []
            arr_t = _MIB_IPNET_ROW2 * n
            rows = ctypes.cast(ctypes.addressof(tbl.contents.Table),
                               ctypes.POINTER(arr_t)).contents
            for r in rows:
                plen = int(r.PhysicalAddressLength or 0)
                if plen <= 0:
                    continue
                mac = "".join("%02X" % r.PhysicalAddress[i] for i in range(plen))
                fam = r.Address.ipv4.sin_family
                if fam == 2:
                    ip = ".".join(str(b) for b in r.Address.ipv4.sin_addr)
                elif fam == 23:
                    ip = ipaddress.IPv6Address(bytes(r.Address.ipv6.sin6_addr)).compressed
                else:
                    continue
                out.append((ip, mac))
        finally:
            try:
                api.FreeMibTable(tbl)
            except Exception:
                pass
    except Exception:
        return []
    return out


def neighbor_map(max_age=_TTL):
    """返回 (ip2mac, mac2v4, mac2v6)，带短期缓存避免每次请求重复读取。"""
    now = time.time()
    with _LOCK:
        if _CACHE["data"] is not None and (now - _CACHE["at"]) < max_age:
            return _CACHE["data"]
    ip2mac, mac2v4, mac2v6 = {}, {}, {}
    for fam in (2, 23):
        for ip, mac in _read(fam):
            ip2mac[norm(ip) or ip] = mac
            if ":" in ip:
                mac2v6.setdefault(mac, set()).add(ip)
            else:
                mac2v4.setdefault(mac, set()).add(ip)
    data = (ip2mac, mac2v4, mac2v6)
    with _LOCK:
        _CACHE["at"] = now
        _CACHE["data"] = data
    return data


def _pick6(ips):
    """从多个 IPv6 中挑一个：全局 > ULA/私网 > 链路本地；排除组播/未指定/回环。"""
    cand = []
    for a in ips:
        try:
            v = ipaddress.IPv6Address(a)
        except Exception:
            continue
        if v.is_multicast or v.is_unspecified or v.is_loopback:
            continue
        rank = 0 if v.is_global else (1 if v.is_private else (2 if v.is_link_local else 3))
        cand.append((rank, v.compressed))
    cand.sort()
    return cand[0][1] if cand else ""


def _pick4(ips):
    for a in sorted(ips):
        try:
            v = ipaddress.IPv4Address(a)
        except Exception:
            continue
        if v.is_multicast or v.is_unspecified or v.is_loopback:
            continue
        return v.compressed
    return ""


def peer_pair(ip):
    """给定来源 IP，返回该主机在同一网段的 (ipv4, ipv6) 配对（尽力而为）。"""
    n = norm(ip)
    if not n:
        return "", ""
    v4 = n if ":" not in n else ""
    v6 = n if ":" in n else ""
    try:
        ip2mac, mac2v4, mac2v6 = neighbor_map()
        mac = ip2mac.get(n)
        if mac:
            if not v6 and mac2v6.get(mac):
                v6 = _pick6(mac2v6[mac])
            if not v4 and mac2v4.get(mac):
                v4 = _pick4(mac2v4[mac])
    except Exception:
        pass
    return v4, v6
