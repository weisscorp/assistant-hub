#!/usr/bin/env python3
"""Download a pinned RNNoise model only; never opens a recording device."""
import hashlib,sys,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from voice_cleanup import DEFAULT_MODEL,MODEL_URL,MODEL_SHA256
from recording_archive import atomic_text

def install():
    data=urllib.request.urlopen(MODEL_URL,timeout=30).read(2_000_000)
    if hashlib.sha256(data).hexdigest()!=MODEL_SHA256:raise ValueError('RNNoise model checksum mismatch')
    DEFAULT_MODEL.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_text(DEFAULT_MODEL,data.decode('ascii'))
    print('Pinned RNNoise voice model installed')

if __name__=='__main__':install()
