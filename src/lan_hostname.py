#!/usr/bin/env python3
"""Small best-effort mDNS hostname advertiser used by the local ranker."""
from __future__ import annotations
import socket, struct, threading, time

_MDNS=("224.0.0.251",5353)

def _encode_name(name:str)->bytes:
    out=bytearray()
    for part in name.strip(".").split("."):
        raw=part.encode("utf-8")[:63];out.append(len(raw));out.extend(raw)
    out.append(0);return bytes(out)

def _local_ipv4()->str:
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("192.0.2.1",9))
        ip=s.getsockname()[0];s.close()
        if ip and not ip.startswith(("127.","169.254.")):return ip
    except OSError:pass
    return ""

class _Advertiser:
    def __init__(self,hostname:str,port:int):
        safe="".join(c if c.isalnum() or c=="-" else "-" for c in hostname.strip())[:63].strip("-") or "artist-ranker"
        self.name=safe+".local";self.port=int(port);self.stop_event=threading.Event()
        self.thread=threading.Thread(target=self._run,name="ranker-mdns-hostname",daemon=True);self.thread.start()
    def stop(self):self.stop_event.set()
    def _packet(self,ip:str)->bytes:
        header=struct.pack("!HHHHHH",0,0x8400,0,1,0,0)
        answer=_encode_name(self.name)+struct.pack("!HHIH",1,1,120,4)+socket.inet_aton(ip)
        return header+answer
    def _run(self):
        while not self.stop_event.is_set():
            ip=_local_ipv4()
            if ip:
                try:
                    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
                    s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,255)
                    s.sendto(self._packet(ip),_MDNS);s.close()
                except OSError:pass
            self.stop_event.wait(30)

def start_lan_hostname_advertiser(hostname:str,port:int):
    return _Advertiser(hostname,port)
