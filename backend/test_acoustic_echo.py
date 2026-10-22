import tempfile,unittest
from pathlib import Path
import numpy as np
from scipy.signal import lfilter
from acoustic_echo import cancel_echo,export_clean,suppress_correlated_residue,RATE

class AcousticEchoTests(unittest.TestCase):
    def setUp(self):
        import os
        from unittest.mock import patch
        from voice_cleanup import model_path
        self.voice_environment=patch.dict(os.environ,{'VOICE_CLEANUP_DISABLE':'1'})
        self.voice_environment.start();model_path.cache_clear()
    def tearDown(self):
        from voice_cleanup import model_path
        self.voice_environment.stop();model_path.cache_clear()

    def test_delayed_room_echo_reduced_while_double_talk_survives(self):
        rng=np.random.default_rng(42);n=RATE*12
        ref=lfilter([1],[1,-.8],rng.normal(0,.06,n)).astype('float32')
        echo=np.zeros(n,dtype='float32');delay=2400
        echo[delay:]=.65*ref[:-delay]
        echo[delay+640:]+=.18*ref[:-delay-640]
        near=np.zeros(n,dtype='float32')
        near[6*RATE:]=lfilter([1],[1,-.65],rng.normal(0,.045,6*RATE))
        mic=echo+near;before=mic.copy();clean=cancel_echo(mic,ref)
        # Converged cancellation, then independent near speech during double talk.
        quiet=slice(3*RATE,6*RATE);talk=slice(8*RATE,None)
        reduction=10*np.log10(np.mean(echo[quiet]**2)/np.mean(clean[quiet]**2))
        self.assertGreater(reduction,12)
        self.assertLess(np.mean((clean[talk]-near[talk])**2),np.mean(echo[talk]**2)*.3)
        gain=np.dot(clean[talk],near[talk])/np.dot(near[talk],near[talk])
        self.assertGreater(gain,.85);self.assertLess(gain,1.15)
        np.testing.assert_array_equal(mic,before);self.assertEqual(len(clean),n)

    def test_silence_unrelated_and_missing_reference_do_not_erase_microphone(self):
        rng=np.random.default_rng(10);mic=rng.normal(0,.1,RATE*4).astype('float32')
        for ref in (np.zeros_like(mic),np.array([],dtype='float32'),rng.normal(0,.1,len(mic))):
            np.testing.assert_array_equal(cancel_echo(mic,ref),mic)

    def test_stereo_speakers_have_distinct_room_paths(self):
        rng=np.random.default_rng(123);n=RATE*10
        left=lfilter([1],[1,-.75],rng.normal(0,.05,n)).astype('float32')
        right=lfilter([1],[1,-.6],rng.normal(0,.05,n)).astype('float32')
        reference=np.column_stack([left,right]);mic=np.zeros(n,dtype='float32')
        mic[1200:]+=.6*left[:-1200];mic[1800:]+=.4*right[:-1800]
        clean=cancel_echo(mic,reference);tail=slice(3*RATE,None)
        self.assertLess(np.mean(clean[tail]**2),np.mean(mic[tail]**2)*.03)

    def test_opposite_phase_stereo_still_detects_echo(self):
        rng=np.random.default_rng(11);x=rng.normal(0,.05,RATE*6).astype('float32')
        reference=np.column_stack([x,-x]);mic=np.zeros_like(x);mic[900:]=.5*x[:-900]
        clean=cancel_echo(mic,reference)
        self.assertLess(np.mean(clean[2*RATE:]**2),np.mean(mic[2*RATE:]**2)*.03)

    def test_residual_suppression_does_not_shift_near_voice(self):
        rng=np.random.default_rng(9);ref=rng.normal(0,.03,RATE*5).astype('float32')
        near=rng.normal(0,.06,len(ref)).astype('float32');mic=near+.5*ref
        clean=cancel_echo(mic,ref)
        from scipy.signal import correlate,correlation_lags
        c=correlate(clean,near,method='fft');lag=correlation_lags(len(clean),len(near))[np.argmax(c)]
        self.assertEqual(lag,0);self.assertEqual(len(clean),len(mic))

    def test_spectral_residue_reduces_reference_without_erasing_near_speech(self):
        rng=np.random.default_rng(76);n=RATE*8
        far=lfilter([1],[1,-.7],rng.normal(0,.05,n)).astype('float32')
        near=lfilter([1],[1,-.5],rng.normal(0,.025,n)).astype('float32')
        residual=.65*far;clean=suppress_correlated_residue(near+residual,far)
        center=slice(RATE,-RATE)
        # Subtract the known independent near voice when scoring echo reduction.
        error=clean[center]-near[center]
        self.assertLess(np.mean(error**2),np.mean(residual[center]**2)*.65)
        gain=np.dot(clean[center],near[center])/np.dot(near[center],near[center])
        self.assertGreater(gain,.9)
        self.assertEqual(len(clean),n)

    def test_headphones_keep_quiet_mic_with_loud_stereo_playback_unchanged(self):
        rng=np.random.default_rng(101);n=RATE*7
        mic=rng.normal(0,.008,n).astype('float32')
        stereo=rng.normal(0,.2,(n,2)).astype('float32')
        np.testing.assert_array_equal(cancel_echo(mic,stereo),mic)

    def test_negative_capture_skew_and_changed_volume(self):
        rng=np.random.default_rng(7);ref=rng.normal(0,.1,RATE*10).astype('float32')
        mic=np.zeros_like(ref);mic[:-1600]=ref[1600:]*.4;mic[5*RATE:]*=1.5
        clean=cancel_echo(mic,ref)
        self.assertLess(np.mean(clean[8*RATE:-RATE]**2),np.mean(mic[8*RATE:-RATE]**2)*.1)

    def test_export_keeps_raw_bytes_and_matching_clean_text_basename(self):
        import soundfile as sf
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);mic=root/'record_микрофон.wav';ref=root/'record_звук-Mac.wav'
            rng=np.random.default_rng(6);samples=rng.normal(0,.05,RATE*2)
            sf.write(mic,samples,RATE);sf.write(ref,samples,RATE)
            original=mic.read_bytes();reference=ref.read_bytes();clean=export_clean(mic,ref)
            self.assertEqual(mic.read_bytes(),original);self.assertEqual(ref.read_bytes(),reference)
            self.assertEqual(sf.info(clean).frames,len(samples));self.assertEqual(clean.stat().st_mode&0o777,0o600)
            self.assertEqual(clean.name,'record_микрофон-без-эха.wav')

    def test_live_closed_mic_uses_reference_without_changing_corpus(self):
        import sys,json,uuid
        import soundfile as sf
        from types import SimpleNamespace
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from speech_live import transcribe_pending
        with tempfile.TemporaryDirectory() as work:
            directory=Path(work);session=str(uuid.uuid4());blobs=directory/'blobs';blobs.mkdir()
            rng=np.random.default_rng(8);samples=rng.normal(0,.04,RATE*3)
            sf.write(directory/'mic-000001.raw.caf',samples,RATE,format='CAF')
            sf.write(directory/'system-000001.raw.caf',samples,RATE,format='CAF')
            source=blobs/'synthetic.wav';sf.write(source,samples,RATE);before=source.read_bytes()
            row={'id':str(uuid.uuid5(uuid.UUID(session),'mic-000001.ready.wav')),'sessionId':session,
                 'deleted':False,'origin':'desktop-mic','rawSha256':'synthetic','createdAt':3000,'durationMs':3000}
            store=SimpleNamespace(rows=lambda:[row],audio=SimpleNamespace(blobs=blobs))
            received=[]
            def transcribe(audio,**kwargs):
                received.append(audio);return [SimpleNamespace(text='synthetic')],None
            output=directory/'live-transcripts.jsonl'
            transcribe_pending(store,SimpleNamespace(transcribe=transcribe),session,output,directory)
            self.assertIsInstance(received[0],np.ndarray)
            self.assertTrue(json.loads(output.read_text())['echoReferenceUsed'])
            self.assertEqual(source.read_bytes(),before)

    def test_final_transcription_uses_clean_mic_and_preserves_both_originals(self):
        import sys, json
        import soundfile as sf
        from types import SimpleNamespace
        from recording_archive import prepare
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import finish
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);directory=root/'capture';directory.mkdir();archive=root/'archive'
            meta=prepare(directory,archive,'synthetic',1700000000,['mic','system'])
            rng=np.random.default_rng(17);samples=rng.normal(0,.05,RATE*3)
            files=[]
            for label in ('микрофон','звук-Mac'):
                path=archive/(meta['base']+'_'+label+'.wav');sf.write(path,samples,RATE);files.append(path)
            before=[p.read_bytes() for p in files]
            meta['phase']='ready';(directory/'archive.json').write_text(json.dumps(meta))
            calls=[]
            def transcribe(path,**kwargs):
                calls.append(Path(path));return [SimpleNamespace(text='synthetic text')],None
            result=finish(directory,SimpleNamespace(transcribe=transcribe),refresh_echo=True)
            self.assertEqual(result['phase'],'ready');self.assertEqual(result['echoVersion'],3)
            self.assertEqual([p.read_bytes() for p in files],before)
            self.assertTrue(calls[0].stem.endswith('-без-эха'));self.assertEqual(calls[1],files[1])
            self.assertEqual(calls[0].with_suffix('.txt').read_text(),'synthetic text\n')
            self.assertEqual(files[0].with_suffix('.txt').read_text(),'synthetic text\n')
            self.assertEqual(len(result['files']),3)

if __name__=='__main__':unittest.main()

