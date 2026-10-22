"""Shared local ASR policy for Russian/English recordings (faster-whisper 1.2.1)."""
from copy import copy

LANGUAGES=('ru','en')


class LanguageDetector:
    """Limit language detection, leaving decoding and original scores unchanged.

    Accented English can otherwise be classified as Greek by Whisper-small.
    The pinned faster-whisper version calls this detector for both the initial
    detection and every multilingual decoding window. No global monkey-patch.
    """
    def __init__(self, model, languages):
        self.model=model
        self.tokens={f'<|{language}|>' for language in languages}

    def __getattr__(self, name):
        return getattr(self.model,name)

    def detect_language(self, *args, **kwargs):
        batches=self.model.detect_language(*args,**kwargs)
        filtered=[sorted((pair for pair in batch if pair[0] in self.tokens),key=lambda pair:pair[1],reverse=True) for batch in batches]
        if any(not batch for batch in filtered):
            raise ValueError('ASR detector returned no supported recording language')
        return filtered


def transcribe_audio(model, audio, *, preview=False, vad_filter=True, word_timestamps=None, languages=LANGUAGES):
    # Keep the adapter alive in the lazy generator without changing the shared
    # model. Auto alone fixes the language from the first window; multilingual
    # re-detects after a Russian mic check or later language switches as well.
    decoder=model
    if getattr(model,'model',None) is not None and languages:
        decoder=copy(model)
        decoder.model=LanguageDetector(model.model,languages)
    return decoder.transcribe(
        audio, language=None, multilingual=True, task='transcribe',
        beam_size=1 if preview else 5, vad_filter=vad_filter,
        condition_on_previous_text=False,
        word_timestamps=not preview if word_timestamps is None else word_timestamps,
    )


def recognition_metadata(info):
    # TranscriptionInfo describes initial detection, not every word's language.
    return {
        'languageMode':'auto-per-window', 'languages':list(LANGUAGES),
        'task':'transcribe', 'initialLanguage':getattr(info,'language',None),
        'initialLanguageProbability':getattr(info,'language_probability',None),
    }
