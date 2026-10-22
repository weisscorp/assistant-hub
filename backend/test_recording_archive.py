import json,tempfile,subprocess,unittest,uuid,sys
from pathlib import Path
from types import SimpleNamespace
from recording_archive import prepare,write_partial,export_audio
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from recording_finish import finish

class ArchiveTests(unittest.TestCase):
    def test_full_audio_and_txt_share_dated_name_and_final_text_wins_over_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'session';directory.mkdir();archive=root/'records';session=str(uuid.uuid4())
            meta=prepare(directory,archive,session,1700000000,['mic'])
            for index in (1,2):
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=0.3','-ar','48000','-ac','2','-c:a','pcm_f32le',str(directory/f'mic-{index:06}.raw.caf')],check=True)
            write_partial(directory,[{'id':'synthetic','origin':'desktop-mic','text':'предварительная фраза'}])
            text=archive/(meta['base']+'_микрофон.txt');self.assertIn('предварительная',text.read_text())
            model=SimpleNamespace(transcribe=lambda *args,**kw:([SimpleNamespace(text='Полный синтетический транскрипт')],None))
            result=finish(directory,model)
            self.assertEqual('ready',result['phase']);wav=text.with_suffix('.wav');self.assertTrue(wav.is_file())
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(wav)]))
            self.assertEqual('48000',info['streams'][0]['sample_rate']);self.assertEqual(2,info['streams'][0]['channels'])
            self.assertAlmostEqual(.6,float(info['format']['duration']),places=2)
            self.assertEqual('Полный синтетический транскрипт\n',text.read_text())
            write_partial(directory,[{'id':'stale','origin':'desktop-mic','text':'устаревшая фраза'}])
            self.assertEqual('Полный синтетический транскрипт\n',text.read_text())
            self.assertEqual(2,len(list(directory.glob('*.raw.caf'))))

    def test_meter_tracks_new_pcm_tail_without_recording_hardware(self):
        import wave,struct
        from audio_meter import tail_levels
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'mic-000001.part.wav'
            def write(amplitude):
                with wave.open(str(path),'wb') as audio:
                    audio.setparams((1,2,16000,0,'NONE','not compressed'))
                    audio.writeframes(struct.pack('<h',amplitude)*3200)
            write(1000);quiet=max(tail_levels(path));write(10000);loud=max(tail_levels(path))
            self.assertGreater(loud,quiet*5);self.assertEqual(32,len(tail_levels(path)))
            write(0);self.assertEqual(0,max(tail_levels(path)))

if __name__=='__main__':unittest.main()
