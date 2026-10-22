"""Synthetic language detection; never capture or play real device audio."""
import unittest
from types import SimpleNamespace as NS
from speech_recognition import LanguageDetector,transcribe_audio,recognition_metadata


class Detector:
    def detect_language(self, window):
        # Simulate accented English mistaken for Greek, then Russian speech.
        return [[('<|el|>',.95),('<|en|>',.04),('<|ru|>',.01)] if window=='english'
                else [('<|ru|>',.9),('<|en|>',.08),('<|el|>',.02)]]


class Decoder:
    def __init__(self):
        self.model=Detector()

    def transcribe(self, audio, **options):
        def segments():
            for window in audio:
                yield self.model.detect_language(window)[0][0]
        return segments(),options


class SpeechRecognitionTests(unittest.TestCase):
    def test_accent_does_not_select_an_unrequested_language_or_inflate_confidence(self):
        detector=LanguageDetector(Detector(),('ru','en'))
        self.assertEqual(detector.detect_language('english'),[[('<|en|>',.04),('<|ru|>',.01)]])

    def test_lazy_decode_can_switch_languages_without_mutating_shared_model(self):
        model=Decoder();original=model.model
        segments,options=transcribe_audio(model,['english','russian','english'])
        self.assertIs(model.model,original)
        self.assertEqual(list(segments),[('<|en|>',.04),('<|ru|>',.9),('<|en|>',.04)])
        self.assertIs(model.model,original)
        self.assertIsNone(options['language']);self.assertTrue(options['multilingual'])
        self.assertEqual(options['task'],'transcribe');self.assertTrue(options['word_timestamps'])

    def test_preview_and_completed_live_parts_keep_different_timing_costs(self):
        model=Decoder()
        _,partial=transcribe_audio(model,[],preview=True)
        _,completed=transcribe_audio(model,[],preview=True,word_timestamps=True,vad_filter=False)
        self.assertFalse(partial['word_timestamps']);self.assertEqual(partial['beam_size'],1)
        self.assertTrue(completed['word_timestamps']);self.assertFalse(completed['vad_filter'])

    def test_initial_detection_is_not_mislabelled_as_language_of_entire_recording(self):
        meta=recognition_metadata(NS(language='ru',language_probability=.8))
        self.assertEqual(meta['initialLanguage'],'ru')
        self.assertEqual(meta['languages'],['ru','en'])
        self.assertEqual(meta['languageMode'],'auto-per-window')
        self.assertNotIn('language',meta)

    def test_empty_supported_detection_fails_instead_of_silently_forcing_a_language(self):
        with self.assertRaises(ValueError):
            LanguageDetector(Detector(),('fr',)).detect_language('english')


if __name__=='__main__':unittest.main()
