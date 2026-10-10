#!/usr/bin/env python3
"""Outbound display-only Coucou notification worker.

Receives display-only text, never shell commands or executable URLs. A lost
receipt is retried with the same lease; the notification is never resubmitted.
"""
import argparse
import ctypes
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid
from hub_node import NodeClient
from coucou_events import wire_event, write_event


def presence():
    """Read OS input idleness, no capture, microphone or device interaction."""
    try:
        raw=subprocess.check_output(['ioreg','-c','IOHIDSystem','-d','1','-r'],timeout=2,text=True)
        idle=int(re.search(r'"HIDIdleTime"\s*=\s*(\d+)',raw).group(1))//1_000_000_000
        # The CoreGraphics session dictionary exposes lock state without screenshots.
        cg=ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cg.CGSessionCopyCurrentDictionary.restype=ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype=ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes=[ctypes.c_void_p,ctypes.c_void_p];cf.CFDictionaryGetValue.restype=ctypes.c_void_p
        cf.CFBooleanGetValue.argtypes=[ctypes.c_void_p];cf.CFBooleanGetValue.restype=ctypes.c_bool
        cf.CFRelease.argtypes=[ctypes.c_void_p]
        session=cg.CGSessionCopyCurrentDictionary()
        if not session:return {'active':False,'idle_seconds':86400}
        key=cf.CFStringCreateWithCString(None,b'CGSSessionScreenIsLocked',0x08000100)
        try:
            value=cf.CFDictionaryGetValue(session,key)
            locked=bool(value and cf.CFBooleanGetValue(value))
        finally:cf.CFRelease(key);cf.CFRelease(session)
        return {'active':idle<120 and not locked,'idle_seconds':min(86400,idle)}
    except (OSError,subprocess.SubprocessError,AttributeError,ValueError):
        return {'active':False,'idle_seconds':86400}


def deliver(client, identity, event, coucou_socket, device):
    hook={'version':1,'deviceId':device,'conversationId':event['id'],
          'kind':'assistant.message','text':(event['title']+'\n'+event['body'])[:4000]}
    written=write_event(coucou_socket,wire_event(hook,'hub-events'))
    status='submitted' if written else 'unknown'
    receipt={'client':identity,'id':event['id'],'lease':event['lease'],
             'status':status,'coucou_written':written}
    deadline=time.monotonic()+45
    while True:
        try:
            return client.request('/api/deliveries/receipt',receipt)
        except OSError as error:
            if getattr(error,'code',None) in {400,401,403,409} or time.monotonic()>deadline:raise
            time.sleep(1)  # Only the receipt; do not display/play anything twice.


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',required=True);p.add_argument('--token-file',required=True)
    p.add_argument('--id',default='desktop:local-host');p.add_argument('--coucou-socket',type=Path,required=True)
    p.add_argument('--device',required=True,help='Stable local UUID for Coucou hook identities')
    args=p.parse_args();uuid.UUID(args.device)
    client=NodeClient(args.url,args.token_file)
    try:
        last_presence=0
        while True:
            try:
                if time.monotonic()-last_presence>15:
                    activity=presence()
                    client.request('/api/presence',{'id':args.id,'kind':'desktop','label':'Coucou desktop',
                        'active':activity['active'],'idle_seconds':activity['idle_seconds']})
                    last_presence=time.monotonic()
                # Keep messages queued while Coucou is closed. Never replay an uncertain write.
                available=args.coucou_socket.exists() and stat.S_ISSOCK(args.coucou_socket.stat().st_mode)
                if not available:time.sleep(2);continue
                event=client.request('/api/deliveries/claim',{'client':args.id})
                if event:deliver(client,args.id,event,args.coucou_socket,args.device)
                time.sleep(2)
            except (OSError,ValueError,KeyError):
                print('Desktop notification connection unavailable; no display action repeated',flush=True)
                time.sleep(5)
    except KeyboardInterrupt:pass


if __name__=='__main__':main()
