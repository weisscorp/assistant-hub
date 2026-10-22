"""Reference-based AEC for derived ASR audio. Never writes capture originals."""
import ctypes as C
import ctypes.util
import os
from pathlib import Path
import subprocess
import tempfile

RATE = 16000
VERSION = 3


def _library():
    candidates = [os.environ.get('SPEEXDSP_LIBRARY'), ctypes.util.find_library('speexdsp'),
                  '/opt/homebrew/opt/speexdsp/lib/libspeexdsp.dylib',
                  '/usr/local/opt/speexdsp/lib/libspeexdsp.dylib']
    for path in filter(None, candidates):
        try:
            lib = C.CDLL(path)
            lib.speex_echo_state_init.argtypes = [C.c_int, C.c_int]
            lib.speex_echo_state_init.restype = C.c_void_p
            lib.speex_echo_state_init_mc.argtypes = [C.c_int,C.c_int,C.c_int,C.c_int]
            lib.speex_echo_state_init_mc.restype = C.c_void_p
            lib.speex_preprocess_state_init.argtypes = [C.c_int,C.c_int]
            lib.speex_preprocess_state_init.restype = C.c_void_p
            lib.speex_preprocess_state_destroy.argtypes = [C.c_void_p]
            lib.speex_preprocess_ctl.argtypes = [C.c_void_p,C.c_int,C.c_void_p]
            lib.speex_preprocess_run.argtypes = [C.c_void_p,C.c_void_p]
            lib.speex_echo_state_destroy.argtypes = [C.c_void_p]
            lib.speex_echo_ctl.argtypes = [C.c_void_p, C.c_int, C.c_void_p]
            lib.speex_echo_cancellation.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
            return lib
        except OSError:
            continue
    raise RuntimeError('Для эхоподавления установите SpeexDSP: brew install speexdsp')


def suppress_correlated_residue(clean, reference):
    """Conservative spectral subtraction of only reference-correlated residue.

    Near speech can protect an entire time frame in the broadband double-talk
    detector. A per-band coherence check still removes speaker residue in other
    frequency bands. Independent speech/noise has no reliable phase relationship
    to the system reference, so it receives no subtraction.
    """
    import numpy as np
    from scipy.signal import stft,istft
    from scipy.ndimage import uniform_filter1d
    if len(clean)<1024:return clean.copy()
    kwargs={'nperseg':512,'noverlap':384,'window':'hann'}
    _,_,near=stft(clean,**kwargs)
    reference=np.asarray(reference)
    if reference.ndim==1:reference=reference[:,None]
    def smooth(values):return uniform_filter1d(values,65,axis=-1,mode='nearest')
    near_power=smooth(np.abs(near)**2)
    best=np.zeros(near.shape,dtype=np.float32);estimate=np.zeros_like(near)
    for channel in range(reference.shape[1]):
        _,_,echo=stft(reference[:,channel],**kwargs)
        cross=smooth(near.real*echo.real+near.imag*echo.imag)+1j*smooth(near.imag*echo.real-near.real*echo.imag)
        echo_power=smooth(np.abs(echo)**2)
        coherence=np.clip(np.abs(cross)**2/(near_power*echo_power+1e-18),0,1)
        use=coherence>best
        estimate=np.where(use,cross/(echo_power+1e-12)*echo,estimate)
        best=np.maximum(best,coherence)
    confidence=np.clip((best-.25)/.35,0,1)
    # At most 6 dB of extra attenuation; do not create spectral holes.
    subtraction=.5*confidence*estimate
    subtraction*=np.minimum(1,.5*np.abs(near)/(np.abs(subtraction)+1e-12))
    _,result=istft(near-subtraction,**kwargs)
    return result[:len(clean)].astype(np.float32)


def cancel_echo(microphone, reference, residual=True):
    """Align independent capture clocks locally, then adapt an acoustic FIR filter.

    No AGC or general noise attenuation: retain near speech and ambient noise.
    Stereo reference channels retain independent acoustic paths. Uncorrelated or
    absent reference is a bypass. Speex handles double talk; raw audio is separate.
    Accepts mono microphone and mono/stereo reference float PCM at 16 kHz;
    output has exactly the microphone length. Residual suppression latency is removed.
    """
    import numpy as np
    from scipy.signal import correlate, correlation_lags, resample_poly
    mic = np.asarray(microphone, dtype=np.float32)
    ref = np.asarray(reference, dtype=np.float32)
    if ref.ndim==1:ref=ref[:,None]
    if ref.ndim!=2 or not 1<=ref.shape[1]<=2:raise ValueError('Reference must be mono or stereo')
    channels=ref.shape[1]
    count = min(len(mic), len(ref))
    if count < RATE // 2:
        return mic.copy()
    # Search both signs: AVAudioEngine and the process tap start independently.
    a = resample_poly(mic[:count], 1, 4);a -= a.mean()
    best=None
    # Probe each speaker as well as the sum: opposite-phase stereo must not
    # disappear from the delay estimator before reaching the stereo AEC.
    for probe in [ref[:count,i] for i in range(channels)]+[ref[:count].mean(axis=1)]:
        b=resample_poly(probe,1,4);b-=b.mean()
        norm=float(np.linalg.norm(a)*np.linalg.norm(b))
        if norm<1e-8:continue
        corr=correlate(a,b,mode='full',method='fft')
        lags=correlation_lags(len(a),len(b));choices=np.flatnonzero(np.abs(lags)<=RATE//4)
        peak=choices[np.argmax(np.abs(corr[choices]))]
        score=abs(float(corr[peak]))/norm
        if best is None or score>best[0]:best=(score,int(lags[peak])*4)
    if best is None or best[0]<.08:return mic.copy()
    # Leave 32 ms ahead of the dominant reflection for the adaptive filter tail.
    lag = best[1] - 512
    aligned = np.zeros((len(mic),channels), dtype=np.float32)
    lo = max(0, lag); hi = min(len(mic), len(ref) + lag)
    if hi > lo:
        aligned[lo:hi] = ref[lo-lag:hi-lag]
    lib = _library(); frame = 256
    state = lib.speex_echo_state_init_mc(frame, 8192, 1, channels)
    preprocessor = None
    if not state:
        raise RuntimeError('Не удалось создать фильтр эхоподавления')
    try:
        rate = C.c_int(RATE)
        lib.speex_echo_ctl(state, 24, C.byref(rate))  # SPEEX_ECHO_SET_SAMPLING_RATE
        if residual:
            preprocessor=lib.speex_preprocess_state_init(frame,RATE)
            if not preprocessor:raise RuntimeError('Не удалось создать подавитель остаточного эха')
            lib.speex_preprocess_ctl(preprocessor,24,C.c_void_p(state))
            for option,value in ((0,1),(18,0),(2,0),(20,-45),(22,0)):
                setting=C.c_int(value);lib.speex_preprocess_ctl(preprocessor,option,C.byref(setting))
        samples = np.rint(np.clip(mic, -1, .999969) * 32768).astype(np.int16)
        playback = np.rint(np.clip(aligned, -1, .999969) * 32768).astype(np.int16)
        # The residual suppressor adds one frame of latency; drain and remove it.
        length=len(mic)+(frame if preprocessor else 0)
        result = np.empty(length, dtype=np.float32)
        linear = np.empty(length, dtype=np.float32)
        # First pass learns the acoustic path; second pass produces the snapshot.
        # Live transcription already works on recorded snapshots, so this adds no
        # capture latency and avoids a fresh convergence burst in every chunk.
        for iteration in range(2):
            for start in range(0, length, frame):
                size = min(frame, length-start)
                near = np.zeros(frame, dtype=np.int16); far = np.zeros((frame,channels),dtype=np.int16); out = near.copy()
                available=max(0,min(size,len(samples)-start))
                near[:available] = samples[start:start+available]; far[:available] = playback[start:start+available]
                lib.speex_echo_cancellation(state, near.ctypes.data, far.ctypes.data, out.ctypes.data)
                linear[start:start+size]=out[:size].astype(np.float32)/32768
                if preprocessor and iteration==1:lib.speex_preprocess_run(preprocessor,out.ctypes.data)
                result[start:start+size] = out[:size].astype(np.float32) / 32768
            if iteration==0:
                # Clear convolution history between the learning and output pass,
                # retaining the learned filter rather than leaking the end into the start.
                zero=np.zeros(frame,dtype=np.int16)
                far_zero=np.zeros((frame,channels),dtype=np.int16);scratch=zero.copy()
                for _ in range(32):
                    lib.speex_echo_cancellation(state,zero.ctypes.data,far_zero.ctypes.data,scratch.ctypes.data)
        if not preprocessor:return result
        from scipy.ndimage import uniform_filter1d
        linear=linear[:len(mic)];suppressed=result[frame:frame+len(mic)]
        # Preserve double talk and ambient noise: extra suppression only when
        # the estimated echo dominates the remaining microphone signal.
        near_power=uniform_filter1d(linear*linear,1024)
        echo=mic-linear;echo_power=uniform_filter1d(echo*echo,1024)
        protect=np.clip((near_power/(echo_power+1e-10)-.1)/.3,0,1)
        output=(protect*linear+(1-protect)*suppressed).astype(np.float32)
        return suppress_correlated_residue(output,aligned)
    finally:
        if preprocessor:lib.speex_preprocess_state_destroy(preprocessor)
        lib.speex_echo_state_destroy(state)


def decode(path, growing=False, channels=1):
    import numpy as np
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
    if growing:
        command += ['-ignore_length', '1']
    command += ['-i', str(path), '-map', '0:a:0', '-f', 'f32le', '-ac', str(channels), '-ar', str(RATE), 'pipe:1']
    data = subprocess.run(command, capture_output=True, check=True, timeout=15).stdout
    samples=np.frombuffer(data[:len(data)//(4*channels)*(4*channels)], dtype=np.float32).copy()
    return samples if channels==1 else samples.reshape(-1,channels)


def capture_reference(directory, index):
    directory = Path(directory)
    for suffix in ('.raw.caf', '.part.caf', '.part.wav'):
        path = directory / ('system-' + index + suffix)
        if path.is_file():
            try:
                return decode(path,growing=suffix=='.part.wav',channels=1 if suffix=='.part.wav' else 2)
            except (OSError,subprocess.SubprocessError):
                # Capture can rename a growing chunk while ffmpeg opens it.
                finalized=directory/('system-'+index+'.raw.caf')
                if finalized.is_file():return decode(finalized,channels=2)
                if path.exists():raise
    return None


def export_clean(microphone, reference):
    """Bounded-memory full recording processing; native WAV files stay unchanged."""
    import soundfile as sf
    microphone = Path(microphone)
    target = microphone.with_name(microphone.stem + '-без-эха.wav')
    temporary = target.with_suffix('.building.wav')
    try:
        with tempfile.TemporaryDirectory(prefix='recording-aec-') as work:
            os.chmod(work, 0o700)
            inputs = []
            for i, source in enumerate((microphone, reference)):
                path = Path(work) / (str(i) + '.wav')
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                                '-ar', str(RATE), '-ac', str(1 if i==0 else 2), '-c:a', 'pcm_f32le', str(path)], check=True, capture_output=True)
                inputs.append(path)
            with sf.SoundFile(inputs[0]) as mic, sf.SoundFile(inputs[1]) as ref:
                with sf.SoundFile(temporary, 'w', samplerate=RATE, channels=1, subtype='PCM_16', format='WAV') as out:
                    os.chmod(temporary, 0o600)
                    for start in range(0, len(mic), 15*RATE):
                        # Re-estimate skew/drift every 15 seconds, with two seconds
                        # of preceding adaptation context before each output block.
                        begin = max(0, start-2*RATE); mic.seek(begin)
                        near = mic.read(start-begin+15*RATE, dtype='float32')
                        ref.seek(min(begin, len(ref)))
                        far = ref.read(len(near), dtype='float32')
                        clean = cancel_echo(near, far)
                        out.write(clean[start-begin:])
            os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
