"""Responsive levels from the recorder's existing growing PCM16 WAV, never opens a device."""
from array import array
import math,sys

def tail_levels(path):
    size=path.stat().st_size
    if size<1728:return None
    with path.open('rb') as stream:
        stream.seek(max(128,size-3200));data=stream.read(3200)
    data=data[:len(data)//2*2];pcm=array('h');pcm.frombytes(data)
    if sys.byteorder!='little':pcm.byteswap()
    if not pcm:return None
    count=len(pcm);levels=[]
    for band in range(32):
        begin=band*count//32;end=max(begin+1,(band+1)*count//32)
        levels.append(min(1,6*math.sqrt(sum((sample/32768)**2 for sample in pcm[begin:end])/(end-begin))))
    return levels
