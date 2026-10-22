import json,tempfile,unittest,sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from export_recording_dataset import export
from timed_transcript import capture_segments,write_timed_outputs

class RecordingDatasetExportTests(unittest.TestCase):
    def test_clips_are_mono_16k_and_session_splits_do_not_leak(self):
        import soundfile as sf
        import numpy as np
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);manifests=[];originals={}
            for i in range(2):
                audio=root/f'session{i}.wav';sf.write(audio,np.random.default_rng(i).normal(0,.01,(48000*3,2)),48000);originals[audio]=audio.read_bytes()
                words=[NS(word=' Синтетический',start=1.,end=1.5,probability=.9),NS(word=' пример',start=1.5,end=2.,probability=.9)]
                segments=[NS(text='Синтетический пример',start=1.,end=2.,words=words,avg_logprob=-.2,no_speech_prob=.01,compression_ratio=1.)]
                raw,doc=capture_segments(segments,audio,f'session{i}','mic');write_timed_outputs(audio,doc,raw);manifests.append(audio.with_suffix('.segments.jsonl'))
            destination=root/'dataset';report=export(manifests+manifests,destination)
            self.assertEqual(report['clips'],2);self.assertEqual(report['train'],1);self.assertEqual(report['validation'],1)
            train=json.loads((destination/'train.jsonl').read_text());valid=json.loads((destination/'validation.jsonl').read_text())
            self.assertNotEqual(train['sessionId'],valid['sessionId'])
            for row in (train,valid):
                info=sf.info(row['audio']);self.assertEqual((info.samplerate,info.channels),(16000,1))
                self.assertAlmostEqual(row['words'][0]['start'],.12,places=3);self.assertFalse(row['humanReviewed'])
            for audio,data in originals.items():self.assertEqual(audio.read_bytes(),data)
            with self.assertRaises(ValueError):export(manifests,destination)

if __name__=='__main__':unittest.main()
