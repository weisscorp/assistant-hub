"""Optional speech-focused RNNoise derivative; capture originals are never changed."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from functools import lru_cache

RATE=16000
VERSION=1
MODEL_COMMIT='3eee541a283fd3b8f81b85b1748e3b9ccbefa04d'
MODEL_SHA256='70bb6685eb0c2a1d18e2918dca3fbfbd39317010b1802eb1b6ea73a92f3fdec0'
MODEL_URL='https://raw.githubusercontent.com/GregorR/rnnoise-models/'+MODEL_COMMIT+'/somnolent-hogwash-2018-09-01/sh.rnnn'
DEFAULT_MODEL=Path.home()/'.local/share/assistant-stack/runtime/rnnoise/sh.rnnn'

@lru_cache(maxsize=1)
def model_path():
    if os.environ.get('VOICE_CLEANUP_DISABLE')=='1':return None
    path=Path(os.environ.get('RNNOISE_MODEL_FILE',str(DEFAULT_MODEL)))
    if not path.is_file():return None
    if path==DEFAULT_MODEL and hashlib.sha256(path.read_bytes()).hexdigest()!=MODEL_SHA256:
        raise RuntimeError('Модель RNNoise повреждена; повторите tools/install_voice_cleanup.py')
    return path


def _filters(model,frames):
    # Escape an ffmpeg filter value, not a shell command. 10ms RNNoise latency
    # is drained and removed; no trimming of pauses or normalization/AGC.
    escaped=str(model).replace('\\','\\\\').replace("'","'\\''").replace(':','\\:')
    return ("aresample=48000,highpass=f=80:p=2,apad=pad_len=480,"
            "arnndn=m='"+escaped+"':mix=0.9,atrim=start_sample=480:end_sample="+str(frames*3+480)+
            ',lowpass=f=7600,aresample=16000')


def enhance_samples(samples):
    import numpy as np
    model=model_path()
    if model is None:return np.asarray(samples,dtype=np.float32).copy()
    audio=np.asarray(samples,dtype='<f4')
    result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','f32le','-ar',str(RATE),'-ac','1','-i','pipe:0',
                           '-af',_filters(model,len(audio)),'-f','f32le','-ac','1','pipe:1'],input=audio.tobytes(),capture_output=True,check=True,timeout=30)
    cleaned=np.frombuffer(result.stdout,dtype='<f4');output=np.zeros(len(audio),dtype=np.float32)
    output[:min(len(output),len(cleaned))]=cleaned[:len(output)]
    if not np.isfinite(output).all():raise ValueError('Некорректный выход RNNoise')
    return output


def export_voice(source):
    import soundfile as sf
    model=model_path()
    if model is None:return None
    source=Path(source);stem=source.stem.replace('_микрофон-без-эха','_голос').replace('_микрофон','_голос')
    if stem==source.stem:stem+='-голос'
    target=source.with_name(stem+'.wav');frames=round(sf.info(source).duration*RATE)
    fd,name=tempfile.mkstemp(prefix='voice-',suffix='.wav',dir=target.parent);os.close(fd)
    try:
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(source),'-af',_filters(model,frames),
                        '-ar',str(RATE),'-ac','1','-c:a','pcm_s16le',name],check=True,capture_output=True)
        if sf.info(name).frames!=frames:raise ValueError('Изменилась длительность очищенной записи')
        os.replace(name,target);return target
    finally:
        Path(name).unlink(missing_ok=True)
