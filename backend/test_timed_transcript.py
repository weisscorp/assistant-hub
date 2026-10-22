"""Synthetic word/segment alignments, never personal recordings."""
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from timed_transcript import capture_segments,corrected_segments,write_timed_outputs,stamp


def fixture():
    words=[NS(word=' Код',start=1.0,end=1.3,probability=.95),NS(word=' на',start=1.3,end=1.5,probability=.96),NS(word=' котли.',start=1.5,end=2.1,probability=.7)]
    return [NS(text=' Код на котли.',start=1.0,end=2.1,words=words,avg_logprob=-.3,no_speech_prob=.01,compression_ratio=1.1)]

class TimedTranscriptTests(unittest.TestCase):
    def test_corrected_term_keeps_original_word_interval_and_probability(self):
        raw,doc=capture_segments(iter(fixture()),'voice.wav','session','mic')
        fixed=corrected_segments(doc,raw.replace('котли','Kotlin'))
        word=fixed['segments'][0]['words'][2]
        self.assertEqual((word['start'],word['end'],word['probability']),(1.5,2.1,.7))
        self.assertEqual(word['word'],'Kotlin.');self.assertEqual(word['asrWord'],'котли.')
        self.assertTrue(word['textCorrected']);self.assertFalse(fixed['humanReviewed'])

    def test_unaligned_added_words_are_not_given_invented_timestamps(self):
        raw,doc=capture_segments(fixture(),'voice.wav','session','mic')
        self.assertEqual(corrected_segments(doc,raw+'Новая мысль.\n')['correctionAlignment'],'needs-realignment')

    def test_txt_srt_manifest_are_paired_to_native_sample_rate(self):
        import soundfile as sf
        import numpy as np
        with tempfile.TemporaryDirectory() as work:
            audio=Path(work)/'record_микрофон.wav';sf.write(audio,np.zeros(48000*4),48000)
            before=audio.read_bytes();raw,doc=capture_segments(fixture(),'voice.wav','session','mic')
            result=write_timed_outputs(audio,doc,raw.replace('котли','Kotlin'))
            self.assertEqual(result['words'],3);self.assertEqual(result['trainingCandidates'],1)
            self.assertIn('[00:00:01.000 – 00:00:02.100] Код на Kotlin.',audio.with_suffix('.txt').read_text())
            self.assertIn('00:00:01,000 --> 00:00:02,100',audio.with_suffix('.srt').read_text())
            row=json.loads(audio.with_suffix('.segments.jsonl').read_text())
            self.assertEqual((row['startSample'],row['endSample'],row['sampleRate']),(48000,100800,48000))
            self.assertEqual(row['splitGroup'],'session');self.assertFalse(row['humanReviewed'])
            self.assertEqual(row['asrText'],'Код на котли.');self.assertEqual(row['text'],'Код на Kotlin.')
            self.assertEqual(audio.read_bytes(),before)

    def test_final_pipeline_and_text_only_review_keep_time_alignment(self):
        import sys
        import numpy as np
        import soundfile as sf
        from unittest.mock import patch
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import finish,review_only
        class Reviewer:
            model='synthetic'
            def propose(self,*args):return [{'before':'котли','after':'Kotlin','reason':'fixture'}]
        def transcribe(*args,**kw):
            self.assertTrue(kw['word_timestamps']);self.assertIsNone(kw['language'])
            self.assertTrue(kw['multilingual']);self.assertEqual(kw['task'],'transcribe')
            return fixture(),NS(language='en',language_probability=.8)
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);directory=root/'session';directory.mkdir();audio=root/'fixture_микрофон.wav'
            sf.write(audio,np.zeros(48000*4),48000)
            (directory/'archive.json').write_text(json.dumps({'phase':'ready','archive':work,'session':'fixture','base':'fixture','sources':['mic'],'files':[str(audio)]}))
            with patch('recording_finish.export_voice',return_value=None):
                result=finish(directory,NS(transcribe=transcribe),True,Reviewer())
            self.assertEqual(result['timestamps'][str(audio.with_suffix('.txt'))]['words'],3)
            self.assertIn('[00:00:01.000',audio.with_suffix('.txt').read_text())
            self.assertIn('Kotlin',audio.with_suffix('.plain.txt').read_text())
            review_only(directory,Reviewer())
            self.assertIn('[00:00:01.000',audio.with_suffix('.txt').read_text())
            doc=json.loads(audio.with_suffix('.timestamps.json').read_text())
            self.assertEqual(doc['segments'][0]['words'][2]['start'],1.5)
            self.assertEqual(doc['recognition']['initialLanguage'],'en')
            row=json.loads(audio.with_suffix('.segments.jsonl').read_text())
            self.assertEqual(row['recognition']['languageMode'],'auto-per-window')

    def test_old_record_retiming_reuses_audio_and_backs_up_prior_text(self):
        import sys
        import numpy as np
        import soundfile as sf
        from unittest.mock import patch
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import retime
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);directory=root/'session';directory.mkdir();audio=root/'fixture_микрофон.wav'
            sf.write(audio,np.zeros(48000*4),48000);original=audio.read_bytes()
            audio.with_suffix('.txt').write_text('Предыдущий синтетический текст')
            (directory/'archive.json').write_text(json.dumps({'phase':'ready','archive':work,'session':'fixture','base':'fixture','sources':['mic'],'files':[str(audio)]}))
            with patch('recording_finish.export_audio',side_effect=AssertionError('must reuse WAV')),patch('recording_finish.export_voice',side_effect=AssertionError('must not clean again')):
                value=retime(directory,NS(transcribe=lambda *args,**kw:(fixture(),None)))
            self.assertEqual(value['phase'],'ready');self.assertIn('[00:00:01.000',audio.with_suffix('.txt').read_text())
            backups=list(directory.glob('before-word-timestamps-*'))
            self.assertEqual((backups[0]/audio.with_suffix('.txt').name).read_text(),'Предыдущий синтетический текст')
            self.assertEqual(audio.read_bytes(),original)

    def test_no_timestamps_means_no_fabricated_dataset(self):
        raw,doc=capture_segments([NS(text='Синтетический текст')],'fixture.wav','session','mic')
        with tempfile.TemporaryDirectory() as work:
            audio=Path(work)/'not-an-audio-file.wav'
            self.assertEqual(write_timed_outputs(audio,doc,raw)['phase'],'unavailable')
            self.assertFalse(audio.with_suffix('.segments.jsonl').exists())

    def test_incremental_alignment_survives_failed_long_asr(self):
        def broken():
            yield fixture()[0]
            raise RuntimeError('synthetic ASR failure')
        with tempfile.TemporaryDirectory() as work:
            checkpoint=Path(work)/'asr-segments.jsonl'
            with self.assertRaises(RuntimeError):capture_segments(broken(),'voice.wav','session','mic',checkpoint)
            row=json.loads(checkpoint.read_text());self.assertEqual(row['words'][2]['start'],1.5)
            self.assertFalse(row['humanReviewed'])

    def test_words_cannot_move_between_timestamped_segments(self):
        segments=fixture()+[NS(text='Готово.',start=3.,end=4.,words=[])]
        raw,doc=capture_segments(segments,'fixture','s','mic')
        self.assertEqual(corrected_segments(doc,'Код.\nНа Kotlin готово.\n')['correctionAlignment'],'needs-realignment')

    def test_long_segment_is_split_only_at_word_boundaries(self):
        from timed_transcript import training_rows
        words=[NS(word=' слово'+str(i),start=float(i),end=i+.8,probability=.9) for i in range(33)]
        segment=NS(text=' '.join(w.word.strip() for w in words),start=0.,end=32.8,words=words,avg_logprob=-.2,no_speech_prob=.01,compression_ratio=1.)
        raw,doc=capture_segments([segment],'voice.wav','session','mic');doc=corrected_segments(doc,raw)
        identity={'durationSeconds':33.,'sampleRate':16000,'audioFrames':528000,'audioSha256':'synthetic','channels':1}
        rows=list(training_rows(doc,Path('raw.wav'),identity))
        self.assertEqual(len(rows),3);self.assertTrue(all(0<r['end']-r['start']<=15 for r in rows))
        self.assertEqual(sum(len(r['words']) for r in rows),33)

    def test_hour_format_does_not_wrap_and_invalid_times_not_used(self):
        self.assertEqual(stamp(7338.901),'02:02:18.901')
        data=fixture();data[0].start=float('nan');raw,doc=capture_segments(data,'fixture','s','mic')
        self.assertFalse(doc['segments'][0]['timingValid'])

if __name__=='__main__':unittest.main()
