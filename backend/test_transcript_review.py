"""Synthetic transcripts only. No actual model server, audio or user files."""
import json,tempfile,unittest,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from transcript_review import chunks,safe_replacement,validated_edits,review_text,review_outputs,ReviewClient

class FakeReviewer:
    model='synthetic-editor'
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def propose(self,text,left,right):
        self.calls.append((text,left,right))
        if self.fail:raise TimeoutError('synthetic timeout')
        if 'котли' in text:return [{'before':'код на котли','after':'код на Kotlin','reason':'термин в Android-контексте'}]
        return []

class TranscriptReviewTests(unittest.TestCase):
    def test_chunks_preserve_every_character_and_context_boundaries(self):
        text=('Синтетический абзац, слово.\n'*300)+'Хвост.'
        bounds=list(chunks(text));self.assertGreater(len(bounds),2)
        self.assertEqual(''.join(text[a:b] for a,b in bounds),text)
        self.assertTrue(all(b-a<=2400 for a,b in bounds))
        self.assertTrue(all(not (text[b-1].isalnum() and text[b].isalnum()) for _,b in bounds[:-1]))

    def test_terms_endings_and_case_allowed_but_inventions_rejected(self):
        self.assertTrue(safe_replacement('код на котли','код на Kotlin'))
        self.assertTrue(safe_replacement('я работа','я работаю'))
        for before,after in [('не удалять','удалять'),('версия 17','версия 18'),('мы обсудили','мы обсудили бюджет'),
                             ('я говорил','мы завершили внедрение'),('да','нет'),('никогда','некогда'),('на\nкотли','\nна Kotlin')]:
            with self.subTest(before=before):self.assertFalse(safe_replacement(before,after))

    def test_ambiguous_substrings_and_overlapping_edits_rejected(self):
        a,r=validated_edits('котли котли',[{'before':'котли','after':'Kotlin'}]);self.assertEqual((a,r),([],1))
        a,r=validated_edits('котлины',[{'before':'котли','after':'Kotlin'}]);self.assertEqual((a,r),([],1))
        proposals=[{'before':'код на котли','after':'код на Kotlin'},{'before':'котли','after':'Kotlin'}]
        a,r=validated_edits('код на котли',proposals);self.assertEqual((len(a),r),(1,1))

    def test_failed_chunks_remain_raw_and_resume_only_retries_failure(self):
        text='Android: код на котли.\n'+('Продолжение синтетического примера.\n'*150)
        class Partial(FakeReviewer):
            def propose(self,target,left,right):
                if not self.calls:
                    self.calls.append((target,left,right));raise TimeoutError()
                return super().propose(target,left,right)
        with tempfile.TemporaryDirectory() as work:
            file=Path(work)/'review.json';first=Partial();out,report=review_text(text,first,file)
            self.assertEqual(out,text);self.assertEqual(report['phase'],'partial')
            retry=FakeReviewer();out,report=review_text(text,retry,file)
            self.assertEqual(len(retry.calls),1);self.assertIn('код на Kotlin',out)
            self.assertEqual(report['phase'],'ready');again=FakeReviewer();self.assertEqual(review_text(text,again,file)[0],out)
            self.assertEqual(again.calls,[])

    def test_original_asr_preserved_and_aliases_deduplicated(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);raw='Android: код на котли.\n';meta={};fake=FakeReviewer()
            paths=[root/'mic.txt',root/'voice.txt'];result=review_outputs(meta,[(p,raw) for p in paths],fake)
            self.assertEqual(len(fake.calls),1);self.assertEqual(meta['textReview']['acceptedEdits'],1)
            for p,text in result:
                self.assertEqual(p.with_suffix('.asr.txt').read_text(),raw);self.assertIn('Kotlin',text)
                self.assertEqual(json.loads(p.with_suffix('.review.json').read_text())['acceptedEdits'],1)

    def test_reported_kotlin_truncations_need_programming_context_and_handle_repeats(self):
        class NoOp(FakeReviewer):
            def propose(self,*args):return []
        text='Разработка Android: код на котли. Ещё пишем на котли.'
        result,report=review_text(text,NoOp())
        self.assertEqual(result.count('Kotlin'),2);self.assertEqual(report['acceptedEdits'],2)
        ambiguous='Записано просто котли, тема неизвестна.'
        self.assertEqual(review_text(ambiguous,NoOp())[0],ambiguous)

    def test_unreachable_model_preserves_text_and_audits_failure(self):
        raw='Нельзя придумывать факты.\n';out,report=review_text(raw,FakeReviewer(fail=True))
        self.assertEqual(out,raw);self.assertEqual(report['phase'],'partial')

    def test_valid_closed_json_at_token_limit_is_not_discarded(self):
        body={'message':{'content':json.dumps({'edits':[{'before':'котли','after':'Kotlin','reason':'term'}]})},'done_reason':'length'}
        with patch('hub_providers.request_json',return_value=body):
            proposals=ReviewClient('http://127.0.0.1:11534','fixture').propose('котли','','')
        self.assertEqual(proposals[0]['after'],'Kotlin')

    def test_only_explicit_local_endpoint_allowed(self):
        for url in ('https://cloud.example','http://example.org','http://127.0.0.1.evil','http://user@127.0.0.1'):
            with self.subTest(url=url),self.assertRaises(ValueError):ReviewClient(url,'fixture')
        ReviewClient('http://127.0.0.1:11534','fixture')

    def test_finalization_runs_editor_after_asr_and_retains_original_text(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import finish
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);session=root/'session';session.mkdir();audio=root/'fixture_микрофон.wav'
            audio.write_bytes(b'synthetic WAV placeholder; fake ASR never opens it')
            meta={'phase':'ready','archive':work,'session':'fixture','base':'fixture','sources':['mic'],'files':[str(audio)]}
            (session/'archive.json').write_text(json.dumps(meta))
            model=SimpleNamespace(transcribe=lambda *a,**k:([SimpleNamespace(text='Android: код на котли.')],None))
            with patch('recording_finish.export_voice',return_value=None):result=finish(session,model,True,FakeReviewer())
            self.assertEqual(result['textReview']['phase'],'ready')
            self.assertIn('Kotlin',audio.with_suffix('.txt').read_text())
            self.assertIn('котли',audio.with_suffix('.asr.txt').read_text())
            self.assertEqual(audio.read_bytes(),b'synthetic WAV placeholder; fake ASR never opens it')

    def test_review_only_never_reads_wav_and_uses_original_on_second_run(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import review_only
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);session=root/'session';session.mkdir();audio=root/'record.wav';audio.write_bytes(b'synthetic inaccessible audio')
            target=audio.with_suffix('.txt');raw='Android: код на котли.\n';target.write_text(raw)
            (session/'archive.json').write_text(json.dumps({'phase':'ready','archive':work,'session':'fixture','files':[str(audio)]}))
            fake=FakeReviewer()
            with patch('recording_finish.export_clean',side_effect=AssertionError('audio must not be read')),patch('recording_finish.export_audio',side_effect=AssertionError('no export')):
                result=review_only(session,fake)
                self.assertEqual(result['textReview']['phase'],'ready');self.assertIn('Kotlin',target.read_text())
                review_only(session,fake)
            self.assertEqual(len(fake.calls),1);self.assertEqual(target.with_suffix('.asr.txt').read_text(),raw)
            self.assertEqual(audio.read_bytes(),b'synthetic inaccessible audio')

if __name__=='__main__':unittest.main()
