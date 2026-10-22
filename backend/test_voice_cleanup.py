"""Synthetic noise/voiced fixtures only; no recording or sound playback."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from voice_cleanup import model_path,enhance_samples,export_voice,DEFAULT_MODEL,RATE

class VoiceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'VOICE_CLEANUP_DISABLE':'0'});self.environment.start();model_path.cache_clear()
    def tearDown(self):self.environment.stop();model_path.cache_clear()

    def test_missing_model_bypasses_without_changing_input(self):
        with tempfile.TemporaryDirectory() as work,patch.dict(os.environ,{'RNNOISE_MODEL_FILE':str(Path(work)/'missing.rnnn')}):
            model_path.cache_clear();x=np.random.default_rng(1).normal(0,.03,RATE).astype('float32')
            np.testing.assert_array_equal(enhance_samples(x),x)
            self.assertIsNone(export_voice(Path(work)/'does-not-exist.wav'))

    @unittest.skipUnless(DEFAULT_MODEL.exists(),'install pinned RNNoise model for DSP integration tests')
    def test_steady_noise_is_reduced_and_duration_does_not_change(self):
        rng=np.random.default_rng(24);noise=rng.normal(0,.02,RATE*3).astype('float32');before=noise.copy()
        output=enhance_samples(noise)
        self.assertEqual(len(output),len(noise));self.assertTrue(np.isfinite(output).all())
        self.assertLess(np.mean(output[RATE:]**2),np.mean(noise[RATE:]**2)*.15)
        np.testing.assert_array_equal(noise,before)

    @unittest.skipUnless(DEFAULT_MODEL.exists(),'install pinned RNNoise model for DSP integration tests')
    def test_voiced_fixture_is_preserved_with_compensated_delay(self):
        from scipy.signal import correlate,correlation_lags
        t=np.arange(RATE*4)/RATE
        envelope=(.5+.5*np.sin(2*np.pi*2.7*t))**2
        voice=sum((.08/k)*np.sin(2*np.pi*140*k*t+0.07*k*np.sin(2*np.pi*3*t)) for k in range(1,25))*envelope
        noise=np.random.default_rng(31).normal(0,.003,len(t))
        output=enhance_samples((voice+noise).astype('float32'))
        c=correlate(output,voice,method='fft');lags=correlation_lags(len(output),len(voice));valid=np.abs(lags)<RATE//10
        lag=lags[np.flatnonzero(valid)[np.argmax(c[valid])]]
        self.assertLess(abs(lag),32) # RNNoise's 10ms delay must not survive.
        gain=np.dot(output,voice)/np.dot(voice,voice)
        self.assertGreater(gain,.4)

    @unittest.skipUnless(DEFAULT_MODEL.exists(),'install pinned RNNoise model for DSP integration tests')
    def test_file_export_preserves_original_and_exact_number_of_samples(self):
        import soundfile as sf
        with tempfile.TemporaryDirectory() as work:
            source=Path(work)/'synthetic_микрофон-без-эха.wav'
            data=np.random.default_rng(3).normal(0,.015,RATE*2+137);sf.write(source,data,RATE)
            original=source.read_bytes();output=export_voice(source)
            self.assertEqual(output.name,'synthetic_голос.wav');self.assertEqual(source.read_bytes(),original)
            self.assertEqual(sf.info(output).frames,len(data));self.assertEqual(output.stat().st_mode&0o777,0o600)

    @unittest.skipUnless(DEFAULT_MODEL.exists(),'install pinned RNNoise model for DSP integration tests')
    def test_full_transcript_uses_voice_derivative_even_without_system_track(self):
        import sys,json
        import soundfile as sf
        from types import SimpleNamespace
        from recording_archive import prepare
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import finish
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);directory=root/'session';directory.mkdir();archive=root/'archive'
            meta=prepare(directory,archive,'synthetic',1700000000,['mic'])
            raw=archive/(meta['base']+'_микрофон.wav')
            sf.write(raw,np.random.default_rng(3).normal(0,.02,RATE),RATE)
            original=raw.read_bytes();meta['phase']='ready';(directory/'archive.json').write_text(json.dumps(meta))
            calls=[]
            def transcribe(path,**kwargs):
                calls.append(Path(path));return [SimpleNamespace(text='synthetic speech')],None
            value=finish(directory,SimpleNamespace(transcribe=transcribe),refresh_echo=True)
            self.assertEqual(value['voiceCleanup'],'rnnoise');self.assertEqual(len(calls),1)
            self.assertTrue(calls[0].stem.endswith('_голос'));self.assertEqual(raw.read_bytes(),original)
            self.assertEqual(calls[0].with_suffix('.txt').read_text(),'synthetic speech\n')

if __name__=='__main__':unittest.main()

