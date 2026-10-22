import hashlib
import io
import tempfile
import unittest
import uuid
import wave
from audio_notes import AudioNotes

class AudioNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=AudioNotes(self.tmp.name); self.device=str(uuid.uuid4())
        output=io.BytesIO()
        with wave.open(output,'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\x00\x00'*16000)
        self.audio=output.getvalue(); self.sha=hashlib.sha256(self.audio).hexdigest()
        self.note={'id':str(uuid.uuid4()),'title':'Планы','text':'Купить молоко', 'createdAt':1,'durationMs':1000,
                   'audioSha256':self.sha,'status':'ready','revision':1}
        self.store.blob(self.sha,self.audio)
    def test_durable_idempotent_recording_and_text(self):
        value=self.store.put(self.device,self.note)
        self.assertEqual(value,self.store.put(self.device,self.note))
        restarted=AudioNotes(self.tmp.name)
        self.assertEqual(self.audio,restarted.blob(self.sha))
        self.assertEqual(value,restarted.read(note_id=self.note['id']))
        self.assertEqual(1,len(restarted.read('молоко')['items']))
    def test_conflicts_and_validation(self):
        self.store.put(self.device,self.note)
        with self.assertRaises(ValueError):self.store.put(str(uuid.uuid4()),self.note)
        with self.assertRaises(ValueError):self.store.put(self.device,{**self.note,'text':'changed'})
        self.store.put(self.device,{**self.note,'text':'new','revision':2})
        self.assertEqual(2,self.store.put(self.device,self.note)['revision'])
        for key,value in [('durationMs',2000),('audioSha256','../file'),('status','bad')]:
            with self.assertRaises((ValueError,KeyError)):self.store.put(self.device,{**self.note,key:value,'revision':3})
        with self.assertRaises(ValueError):self.store.blob('0'*64,self.audio)
    def test_paging_and_literal_search(self):
        for i in range(13):self.store.put(self.device,{**self.note,'id':str(uuid.uuid4()),'createdAt':i+1})
        first=self.store.read(); second=self.store.read(offset=first['next'])
        self.assertEqual(13,len(first['items'])+len(second['items']))
        self.assertIsNone(second['next']); self.assertFalse(self.store.read('%')['items'])

if __name__=='__main__': unittest.main()

class AudioNotesHttpTest(unittest.TestCase):
    def test_authenticated_mobile_sync_library_and_assistant_access(self):
        from test_hub_stack import HubTests
        import urllib.request
        import urllib.error
        fixture=HubTests();fixture.setUp()
        try:
            sample=AudioNotesTest();sample.setUp()
            try:
                note=sample.note;device=sample.device
                request=urllib.request.Request(fixture.url+'/link/'+device+'/audio/'+sample.sha,data=sample.audio,
                    headers={'Authorization':'Bearer '+fixture.token,'Content-Type':'audio/wav'})
                with urllib.request.urlopen(request) as response:self.assertEqual(200,response.status)
                value=fixture.request('/link/'+device+'/audio-notes',note)
                self.assertEqual(note['id'],value['id'])
                self.assertEqual(1,len(fixture.request('/api/audio-notes?query=%D0%9C%D0%9E%D0%9B%D0%9E%D0%9A%D0%9E')['items']))
                with self.assertRaises(urllib.error.HTTPError) as denied:fixture.request('/api/audio-notes',auth=False)
                self.assertEqual(401,denied.exception.code)
                self.assertTrue(fixture.app.store.search_memory('молоко'))
                row=fixture.app.coordinator.submit(fixture.context,'test','fixture','read')
                self.assertTrue(fixture.app.coordinator.call_tool('audio_notes',{'query':'молоко','id':'','offset':0},row,__import__('threading').Event())['items'])
                self.assertEqual(1,len(fixture.request('/link/'+device+'/audio-notes?offset=0')['items']))
                with urllib.request.urlopen(urllib.request.Request(fixture.url+'/api/audio-notes/audio/'+sample.sha,headers={'Authorization':'Bearer '+fixture.token})) as response:
                    self.assertEqual(sample.audio,response.read())
            finally:sample.doCleanups()
        finally:fixture.tearDown()
