"""Synthetic terminology and temporary storage; never touches user glossary."""
import copy,hashlib,json,tempfile,unittest,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import speech_glossary as g
from transcript_review import ReviewClient,review_text,reapply_glossary,TERMS

class GlossaryTests(unittest.TestCase):
    def setUp(self):self.data=json.loads(g.SEED.read_text())
    def test_retrieval_only_sends_matching_entries(self):
        client=ReviewClient('http://localhost:11534','fixture')
        with patch('hub_providers.request_json',return_value={'message':{'content':'{"edits":[]}'}}) as request:
            client.propose('Инфраструктурный тэроформ-проект','','')
        payload=json.loads(request.call_args.args[1]['messages'][1]['content'])
        self.assertEqual([e['canonical'] for e in payload['matched_glossary_entries']],['Terraform'])
        self.assertNotIn('Kubernetes',json.dumps(payload));self.assertNotIn('possible_terms_not_mandatory',payload)
        self.assertEqual(g.relevant('Просто обсуждаем погоду.',self.data),[])

    def test_dictionary_overrides_model_guess_and_protects_english_names(self):
        client=SimpleNamespace(model='fake',terms=TERMS,glossary=self.data,
            propose=lambda *args:[{'before':'тэроформ-проект','after':'термоформ-проект'}, {'before':'Docker','after':'докер'}])
        raw='Инфраструктура: тэроформ-проект, Docker.'
        text,report=review_text(raw,client)
        self.assertEqual(text,'Инфраструктура: Terraform-проект, Docker.')
        self.assertEqual(report['acceptedEdits'],1)
        self.assertEqual(g.edits('Питон ест мышь.','','',self.data),[])
        self.assertEqual(g.edits('Используем термоформу для пластика.','','',self.data),[])

    def test_learning_requires_independent_sessions_and_literal_evidence(self):
        edit={'before':'постгресс','after':'PostgreSQL'}
        # Use a phonetic variant of an existing canonical with a high similarity.
        edit={'before':'доккер','after':'Docker'}
        with tempfile.TemporaryDirectory() as work:
            path=Path(work)/'glossary.json';g.load(path)
            g.observe(path,'Docker', [edit], 'one')
            g.observe(path,'Docker', [edit], 'one')
            doc=g.load(path);self.assertEqual(doc['candidates'][0]['status'],'candidate')
            g.observe(path,'обсуждение', [edit], 'two')
            doc=g.load(path);self.assertEqual(doc['candidates'][0]['status'],'active')
            entry=next(e for e in doc['entries'] if e['canonical']=='Docker');self.assertIn('доккер',entry['aliases'])
            # Novel names can be added too, but no repeated model guess alone is sufficient.
            new={'before':'линкстоун','after':'Linkstone'}
            for session in ('one','two'):g.observe(path,'обсуждение',[new],session)
            self.assertFalse(any(e['canonical']=='Linkstone' for e in g.load(path)['entries']))
            g.observe(path,'Linkstone',[new],'three')
            self.assertTrue(any(e['canonical']=='Linkstone' for e in g.load(path)['entries']))
            self.assertNotIn('обсуждение',path.read_text())

    def test_cached_wrong_term_repaired_without_model(self):
        raw='Костяк репозитория: тэроформ-проект.'
        start=raw.index('тэроформ');before='тэроформ-проект'
        report={'version':1,'rawSha256':hashlib.sha256(raw.encode()).hexdigest(),'model':'old','terms':[],
                'chunks':[{'start':0,'end':len(raw),'state':'ready','edits':[{'start':start,'end':start+len(before),'before':before,'after':'термоформ-проект'}]}], 'phase':'ready'}
        original=copy.deepcopy(report)
        text,fixed=reapply_glossary(raw,report,self.data)
        self.assertIn('Terraform-проект',text);self.assertEqual(report,original)
        self.assertEqual(reapply_glossary(raw,fixed,self.data)[0],text)
        with self.assertRaises(ValueError):reapply_glossary(raw+'!',report,self.data)

    def test_glossary_only_backs_up_text_and_preserves_raw(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from recording_finish import glossary_only
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);session=root/'session';session.mkdir();glossary=root/'glossary.json';g.load(glossary)
            audio=root/'fixture.wav';audio.write_bytes(b'no audio decoder may read this')
            raw='Инфраструктура: тэроформ-проект.';start=raw.index('тэроформ');before='тэроформ-проект'
            audit={'rawSha256':hashlib.sha256(raw.encode()).hexdigest(),'chunks':[{'start':0,'end':len(raw),'state':'ready','edits':[{'start':start,'end':start+len(before),'before':before,'after':'термоформ-проект'}]}]}
            audio.with_suffix('.asr.txt').write_text(raw);audio.with_suffix('.txt').write_text(raw.replace('тэроформ','термоформ'));audio.with_suffix('.review.json').write_text(json.dumps(audit))
            (session/'archive.json').write_text(json.dumps({'phase':'ready','archive':work,'session':'fixture','files':[str(audio)]}))
            glossary_only(session,glossary)
            self.assertIn('Terraform-проект',audio.with_suffix('.txt').read_text())
            self.assertEqual(audio.with_suffix('.asr.txt').read_text(),raw)
            self.assertEqual(audio.read_bytes(),b'no audio decoder may read this')
            backup=list(session.glob('before-glossary-*'));self.assertEqual(len(backup),1)
            self.assertIn('термоформ',(backup[0]/'fixture.txt').read_text())

if __name__=='__main__':unittest.main()
