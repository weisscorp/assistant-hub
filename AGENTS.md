# Contributor instructions

Use synthetic fixtures and temporary state for tests. Never commit personal
knowledge, conversation history, credentials, runtime configuration or captures.
Do not connect to physical devices, install applications, record audio or run
sound checks without a fresh explicit request from their owner. Device tests
must target an explicitly selected disposable emulator. Enable delivery automations
only after an explicit owner request. Preserve third-party authorship and license notices.

## Аудиозаписи, транскрипция и обучающие датасеты

Актуальность этой памятки: 22 октября 2026. При изменении пайплайна обновляй
этот AGENTS.md вместе с кодом. Параметры ниже проверены по реализации;
если код изменился, сначала сверь реализацию, не запускай старые команды вслепую.
Подробности и история функций: `docs/speech-dataset.md` в репозитории assistant-hub.

### Задача и обязательные правила

- Главная цель — сохранить неизменённое шумное сырьё для обучения ASR.
  Очистка, исправление терминов и нарезка создают производные файлы.
  Никогда не перезаписывай исходные WAV/CAF обработанным звуком.
- Микрофон и системный звук — отдельные дорожки. Экран/видео не записываются.
  Разрешение на захват экрана не нужно: используется CoreAudio process tap.
- Не включай запись, микрофон, воспроизведение или проверки физических устройств
  для проверки изменений без отдельного разрешения пользователя. Используй
  синтетические тесты; существующие файлы обрабатывай в рамках запроса пользователя.
- Не публикуй записи/тексты, не переноси их в Git и не выводи содержимое приватной
  конфигурации, токены или capability URL. Корпус не является общей памятью агента.
- Машинная транскрипция и LLM-коррекция — псевдометки, `humanReviewed=false`.
  Не выставляй подтверждение человеком автоматически и не называй их ground truth.
- У исправленного слова сохраняются исходные интервалы. Нельзя придумывать время
  для вставленных слов, переносить слова между ASR-сегментами или менять факты,
  числа и отрицания ради «красивого текста». При потере соответствия нужна
  реальная повторная разметка, статус `needs-realignment` нельзя игнорировать.
- Дорожки/варианты одной записи и одинаковые исходные аудио должны находиться
  в одном split. Не дели соседние куски одной встречи между train и validation.
- Обучение, скачивание большой модели, экспорт ONNX и замена рабочего ASR —
  отдельные операции. Сбор записи сам по себе их не запускает.

### Где код и данные на этом компьютере

- Репозиторий: `/Users/vtikhonov/work/android-app/public/assistant-hub`.
- Coucou (SwiftUI): соседний `/Users/vtikhonov/work/android-app/public/coucou`.
- Приватный стек: `~/.local/share/assistant-stack`; конфигурация `config.json`.
  Не печатай её целиком. Извлекай только необходимые несекретные поля.
- Сессии: `~/.local/share/assistant-stack/state/speech-capture/SESSION_UUID/`.
  Связь с архивными файлами бери из `archive.json`, не угадывай по последнему mtime.
- Архив для пользователя: `~/Documents/Аудиозаписи` либо `recordings_directory`
  в конфигурации. Базовое имя: `YYYY-MM-DD_HH-MM-SS_SESSIONPREFIX`.
- Корпус Hub: `~/.local/share/assistant-stack/state/asr-dataset/`:
  `samples.sqlite` — реестр, `audio/SHA256.wav` — исходные образцы для ASR.
  Это отдельное представление от полноразмерного нативного архива.
- Python распознавания:
  `~/.local/share/assistant-stack/runtime/speech-venv/bin/python` (сейчас Python 3.13).
  Для обучения T-one нужна отдельная среда Python 3.12.
- Рабочий глоссарий:
  `~/.local/share/assistant-stack/state/speech-glossary.json`.
  Версионируемый начальный словарь: `data/speech-glossary.json`.
- Native helper:
  `~/.local/share/assistant-stack/runtime/SpeechDatasetCapture.app/Contents/MacOS/SpeechCapture`.
  Не пересобирай и не переподписывай его ради изменений Python: изменение подписи
  может потребовать повторных системных разрешений.

### Что автоматизировано сейчас

После явно начатой записи сырьё пишется независимо от распознавания.
Флажок «Транскрибация» включает только предварительный текст во время записи.
После остановки автоматически запускаются экспорт полного аудио, очистка
микрофона, полный ASR, коррекция текста и вывод временной разметки/кандидатов
`.segments.jsonl`. Полный ASR выполняется и при выключенном live-флажке.

Финальная физическая нарезка в `audio/` и сборка `train.jsonl`/`validation.jsonl`
пока НЕ запускаются на каждую запись автоматически. Для этого есть явный экспорт
ниже. Large-v3 teacher и дообучение тоже запускаются отдельно. Не сообщай
пользователю, что готовый датасет или обучение создаются автоматически.

Язык захвата не фиксируется русским: `backend/speech_recognition.py` выбирает
ru/en на каждом окне декодирования, включая live и повторный ASR. Просто
`language=None` без `multilingual=True` оставляет язык первого окна на весь файл.
Выбор ограничен ru/en, поскольку small может принять английскую речь с греческим
акцентом за греческий. Это явная текущая область поддержки: для записи на другом
языке нужно расширить список LANGUAGES и проверить качество. Адаптер детектора
рассчитан на закреплённый faster-whisper 1.2.1; при обновлении проверь оба вызова
`detect_language` (начальный и внутри цикла). Оценки вероятности не нормализуются
после фильтрации. В ASR/таймкодах/manifests сохраняется `recognition` с режимом,
списком языков и **начальным**, а не общим языком записи. Редактор сохраняет язык
фраз и не переводит их. Teacher T-one ниже остаётся отдельным русским путём.

Изменения Python-обработчиков подхватывают следующие запуски worker-процессов;
для них не нужна новая сборка Coucou. Уже работающий worker не подхватит правки
на лету. Не прерывай активную запись/долгую обработку ради обновления.

### Технологии и параметры

| Этап | Реализация и текущие параметры |
| --- | --- |
| Нативный захват | `tools/macos/SpeechCapture.swift`; macOS 15+, CoreAudio process tap (звук Mac), AVAudioEngine (микрофон). Нативный PCM/частота/каналы сохраняются в CAF, части примерно по 15 с. Отдельная ASR-копия — WAV PCM16 mono 16 кГц. |
| Управление/экспорт | `backend/desktop_speech_capture.py`, `backend/recording_archive.py`, `tools/recording_finish.py`; независимые процессы, атомарные записи метаданных, файловые flock-блокировки. |
| Live ASR | `tools/speech_live.py`: faster-whisper `base`, CPU int8, 3 потока, автоопределение ru/en на каждом окне, beam=1, `condition_on_previous_text=False`. Снимки растущего WAV примерно каждые 4 с, хвост до 15 с; у завершённых частей есть word timestamps. Задержка CPU не равна времени захвата. |
| Полный ASR | faster-whisper `small`, CPU int8, 3 потока; `language=None`, `multilingual=True`, `task='transcribe'`, выбор между ru/en, `beam_size=5`, `vad_filter=True`, `condition_on_previous_text=False`, `word_timestamps=True`. Микрофон распознаётся по голосовой/очищенной версии, системная дорожка отдельно. |
| Эхоподавление | `backend/acoustic_echo.py`, SpeexDSP через ctypes, версия 3: 16 кГц, stereo reference, frame=256, filter=8192; компенсация сдвига до ±1 с, блоки по 15 с с контекстом адаптации 2 с, коррелированное остаточное подавление. При тихой/несвязанной опоре микрофон проходит без AEC. Наушники не требуют «вычитания» несвязанной системной дорожки. |
| Шумоподавление | `backend/voice_cleanup.py`, версия 1: FFmpeg `arnndn`, модель RNNoise `sh.rnnn`, 48 кГц внутри фильтра, highpass 80 Гц, mix=0.9, компенсация 480 сэмплов, lowpass 7600 Гц, выход 16 кГц mono. Не изменяет оригиналы; при отсутствии модели доступен обход. |
| Редактор текста | `backend/transcript_review.py`; локальный Ollama, текущий URL `http://127.0.0.1:11534`, модель `qwen3:4b-instruct-2507-q4_K_M` (проверь локальную настройку). Куски до 2400 символов, соседний контекст по 500, temperature=0, num_ctx=8192, num_predict=1400; до 8 локальных правок на кусок, строгая валидация и кеш. |
| Глоссарий | `backend/speech_glossary.py`: отдельный JSON. В модель идут только совпавшие записи, максимум 12, а не весь словарь. Известное каноническое имя перекрывает догадку модели. Неоднозначные русские слова требуют контекста. |
| Временная разметка | `backend/timed_transcript.py`: интервалы слов/фраз Whisper относительно начала соответствующего файла; confidence относится к исходному ASR, не к LLM-правке. |
| Экспорт клипов | `tools/export_recording_dataset.py`: SoundFile + SciPy resample_poly, выход WAV PCM16 mono 16 кГц, запас до 0.12 с по краям интервала. |
| Большой teacher | `tools/speech_teacher.py`: faster-whisper `large-v3`, по умолчанию CPU int8, beam=5, VAD, русский, word timestamps. Разметка и отбор для старого T-one пути отличаются от универсального экспорта. |
| Обучение T-one | `tools/train_speech_model.py`: upstream ToneForCTC/PyTorch/Transformers, по умолчанию 3 эпохи, batch=2, CPU, LR=5e-5, accumulation=8. Источники 16 кГц ресемплируются до 8 кГц для этой модели. Не универсальный тренер для любого ASR. |

Зависимости распознавания закреплены в `tools/speech-training-requirements.txt`:
faster-whisper 1.2.1, SoundFile 0.13.1, SciPy 1.16.2, PyAV 16.1.0.
Не обновляй PyAV до 19 без проверки совместимости: faster-whisper 1.2.1 передаёт
удалённый аргумент `metadata_errors`. SpeexDSP — системная библиотека
(Homebrew `speexdsp`, override `SPEEXDSP_LIBRARY`). RNNoise устанавливается явной
командой `tools/install_voice_cleanup.py`, проверяет закреплённый SHA256;
`RNNOISE_MODEL_FILE` меняет путь, `VOICE_CLEANUP_DISABLE=1` отключает этап.
Зависимости отдельной среды обучения: `tools/speech-finetune-requirements.txt`.
Сохраняй сторонние лицензии в `THIRD_PARTY.md`.

### Что содержат файлы

Суффиксы относятся к одному и тому же базовому имени дорожки.

| Файл/каталог | Содержимое и назначение |
| --- | --- |
| `…_микрофон.wav` | Нативная полная микрофонная дорожка с шумом/эхом, оригинал для обучения. Частоту/каналы читай из заголовка, не предполагай 16 кГц. |
| `…_звук-Mac.wav` | Оригинальный системный звук, возможен stereo; отдельный источник и reference AEC. |
| `…_микрофон-без-эха.wav` | Производный микрофон после AEC, mono 16 кГц. |
| `…_голос.wav` | Производный микрофон после AEC (если доступен) и RNNoise, mono 16 кГц. |
| `.txt` | После финализации — исправленные фразы с `[HH:MM:SS.mmm – HH:MM:SS.mmm]`. Во время записи предварительный текст. Старые записи могут быть без меток. |
| `.plain.txt` | Исправленный текст без меток; удобен для чтения/поиска. Не путать с сырым ASR. |
| `.asr.txt` | Исходный текст ASR до языковой коррекции. Правка глоссарием/редактором его не меняет; явный `--retime` создаёт новое распознавание с бэкапом предыдущего. |
| `.asr.json` | Исходные сегменты/слова ASR, char offsets, вероятности, rawText и SHA, источник распознавания, track/session и временная база. Не восстанавливай из уже исправленного TXT. |
| `.asr-segments.jsonl` | Инкрементальный журнал сегментов по мере полного ASR; уже полученные данные остаются при сбое долгой обработки. Новый проход ASR начинает журнал заново. |
| `.review.json` | Аудит/кеш коррекции: хеш сырого текста, модель, границы чанков, правки before/after, причины, rejected, состояние/ошибки. Не считать объяснение модели доказательством правильности. |
| `.timestamps.json` | Полная исходная и исправленная словесная разметка, интервалы, confidence/provenance, SHA/формат соответствующего WAV. |
| `.srt` | Исправленные фразы как субтитры с настоящими интервалами ASR. |
| `.segments.jsonl` | Кандидаты для нарезки: audio и recognitionAudio, SHA, частота/каналы/длительность, sessionId/splitGroup, start/end в секундах, startSample/endSample, asrText/text, слова с вероятностями, labelSource, humanReviewed, candidateForTraining. |
| `.audio-sha256.json` | Кеш SHA аудио, инвалидируется при изменении пути/размера/mtimeNs. Не текстовая разметка. |
| `mic-*.part.caf`, `system-*.part.caf` | Текущие незавершённые нативные части; не перемещать и не обрабатывать как окончательно закрытые файлы. |
| `*-*.raw.caf`, `*-*.raw.json` | Завершённые нативные части и метаданные целостности/формата. |
| `*-*.part.wav`, `*-*.ready.wav` | Растущие/закрытые копии для ASR-корпуса, mono PCM16 16 кГц. Не замена нативным CAF. |
| `archive.json` внутри сессии | Состояние финализации, базовое имя, источники, список архивных WAV, путь recognition source, этапы очистки/ASR/review/timing. Главная точка навигации по сессии. |
| `capture.json`, `permissions.json` | Состояние менеджера захвата и результаты проверки разрешений в корне speech-capture. Не являются аудио/датасетом. |
| `capture.jsonl` | События helper в каталоге сессии. |
| `live-transcripts.jsonl` | Предварительные гипотезы завершённых частей: текст/слова, времена, track, SHA источника, provenance. Не ручные метки. |
| `partial-preview.json` | Последний предварительный текст растущих частей по дорожкам, время обновления и имена частей. |
| `transcription-state.json`, `finalizer.json` | Состояние live-worker и сведения о процессе финализатора; не размеченные примеры. |
| `before-word-timestamps-*`, `before-glossary-*` | Резервные копии производных текстов/метаданных перед явной повторной обработкой. Не включать в обучающий набор как дополнительные записи. |
| `speech-glossary.json` | `entries`: canonical/aliases/context/source; `candidates`: новые соответствия, хеши независимых сессий, literalEvidence/status. Не содержит выдержек бесед. |
| `samples.sqlite` | Реестр образцов корпуса: идентификаторы, сессии, SHA, источники, состояние удаления и проверки/исправления. Не редактировать SQL вручную вместо API. |
| `audio/*.wav` в корпусе Hub | Исходные ASR-образцы корпуса, адресованные хешем. Файл не должен подменяться под прежним SHA. |
| `audio/*.wav` в экспорте | Отдельно нарезанные клипы, mono PCM16 16 кГц, имена по ID. |
| `train.jsonl`, `validation.jsonl` | По одной паре audio/text на строку плюс provenance/session/source SHA и интервалы. У универсального экспорта words относительно начала клипа; start/end остаются относительно полного источника, clipStart/clipEnd включают запас. |
| `report.json` | Размеры выборок, отфильтрованные кандидаты, тип меток, readyToTrain и параметры экспорта. Ненулевые clips не гарантируют наличие независимой validation. |
| `transcripts.jsonl` (teacher) | Аудит предварительной large-v3 разметки, включая не принятые для обучения интервалы и confidence. |
| `checkpoint-*`, `final/`, `comparison.json` (обучение) | Промежуточные/финальные веса и сравнение до/после. Не готовая автоматическая замена Android ONNX. |
| `.*.lock`, временные `.building.wav` | Синхронизация/незавершённая обработка; не удалять при активных процессах и не использовать как готовые данные. |

Для вариантов микрофона ASR может выполняться один раз по `…_голос.wav`, а его
разметка переносится на исходный и AEC WAV с сохранением времени. Поля `audio`
и `recognitionAudio` поэтому различаются. Чтобы учить на шумном голосе, экспортируй
manifest **исходного микрофона**, а не только очищенный `голос`.

### Как собирать датасет из записей

1. Найди нужные сессии через `archive.json`. Дождись `phase=ready`;
   проверь `timestamps`, наличие `.asr.json` и `.segments.jsonl` у выбранной дорожки.
   Старый TXT без меток сам по себе не даёт соответствия звуку.
2. Выбери представление аудио: обычно сырой микрофон для устойчивости к шуму;
   системный звук можно добавить отдельно. Не считай три версии микрофона тремя
   независимыми записями. Для оценки качества нужны разные реальные сессии.
3. Универсальный экспорт принимает только `candidateForTraining=true`.
   Текущий отбор: длительность 1–15 с; avg_logprob >= -1;
   0 <= no_speech_prob < 0.5; 0 < compression_ratio < 2.4;
   вероятность каждого слова 0.1–1, положительные и упорядоченные интервалы,
   соответствие char offsets и отсутствие `needs-realignment`. Полный список
   проверок смотри в `timed_transcript.training_rows`.
4. Передай конкретные manifests и новый/пустой выходной каталог. Не запускай
   глобальный glob по всему архиву с бэкапами и несколькими вариантами дорожек.
5. Проверь report, не смешались ли сессии/одинаковые исходники между split,
   и выборочно соответствие аудио словам. Автоматический split детерминирован:
   связанные сессии объединяются по SHA, примерно пятая часть групп в validation
   (минимум одна, если групп больше одной). С единственной группой validation
   пустая, `readyToTrain=false`; не исправляй это случайной нарезкой одной встречи.

Команды ниже выполняются из корня assistant-hub. Обозначения `/absolute/...`
и `SESSION_UUID` — placeholders: подставляй существующие выбранные пути.

```sh
# Готовая разметка -> клипы и JSONL; --manifest можно повторять для разных сессий.
~/.local/share/assistant-stack/runtime/speech-venv/bin/python \
  tools/export_recording_dataset.py \
  --manifest /absolute/recording_микрофон.segments.jsonl \
  --manifest /absolute/other-session_микрофон.segments.jsonl \
  --output /absolute/new-dataset \
  --labels corrected
```

`--labels corrected` по умолчанию использует исправленные слова, сохраняя asrText.
`--labels asr` использует исходные слова и соответствующий provenance.
Источник проверяется по SHA; при несовпадении не обходи проверку и не подменяй хеш.

```sh
# Старый файл без word timestamps: заново распознать существующий WAV.
# Долго; сохраняет бэкап текстов, не повторяет звуковую очистку.
~/.local/share/assistant-stack/runtime/speech-venv/bin/python \
  tools/recording_finish.py --directory /absolute/speech-capture/SESSION_UUID --retime

# Повторить только LLM-коррекцию, используя исходный ASR и кеш чанков.
~/.local/share/assistant-stack/runtime/speech-venv/bin/python \
  tools/recording_finish.py --directory /absolute/speech-capture/SESSION_UUID --review-only

# Применить словарь к готовому аудиту без повторного ASR и вызова LLM.
~/.local/share/assistant-stack/runtime/speech-venv/bin/python \
  tools/recording_finish.py --directory /absolute/speech-capture/SESSION_UUID --glossary-only
```

`--refresh-echo` — отдельная дорогая пересборка производного звука с новым
полным ASR. Для правки терминов/словаря её НЕ использовать. Обычный запуск
`recording_finish.py --directory ...` завершает сессию; для уже ready возвращает
существующее состояние и сам не пересчитывает результаты.

Альтернативный teacher-путь работает по корпусу Hub, а не по архивным manifests:

```sh
~/.local/share/assistant-stack/runtime/speech-venv/bin/python \
  tools/speech_teacher.py \
  --state ~/.local/share/assistant-stack/state/asr-dataset \
  --output /absolute/new-teacher-dataset \
  --model large-v3 --device cpu --compute-type int8 \
  --sources desktop-mic,desktop-system,phone
```

Первое использование large-v3 может скачать большие веса. Teacher фильтрует
фразы 1–20 с и отбрасывает латиницу/цифры для русского T-one; это отличается от
универсального 1–15 с экспорта, сохраняющего английские термины.
`backend/speech_dataset.py --state ... export /absolute/new-human-export`
— ещё один отдельный путь: экспорт уже проверенных человеком образцов корпуса.

Не передавай универсальный датасет с Kotlin/Terraform напрямую в текущий тренер
T-one: он отвергает латиницу/цифры. Сначала выбери целевую ASR и явную нормализацию
для её алфавита, сохраняя исходные метки. Не удаляй произнесённые слова ради
прохождения фильтра. Для T-one CLI смотри `tools/train_speech_model.py --help`;
`--allow-pseudo-labels` означает явный выбор машинных меток, а не их достоверность.
Не полагайся только на программный guard: проверяй labelSource/humanReviewed
всего набора. После обучения сравни на независимой проверенной выборке;
рабочая модель сама не заменяется.

Пример отдельного обучения после проверки совместимости меток и split
(`/absolute/training-venv/bin/python` — Python 3.12 с finetune-зависимостями):

```sh
/absolute/training-venv/bin/python tools/train_speech_model.py \
  --dataset /absolute/compatible-russian-dataset \
  --output /absolute/new-model \
  --base-model t-tech/T-one --epochs 3 --batch-size 2 \
  --device cpu --allow-pseudo-labels
```

Это пример команды, а не указание автоматически начинать обучение при чтении
AGENTS.md. Для проверенных человеком меток флаг `--allow-pseudo-labels` не нужен.

### Глоссарий и пополнение

Храни словарь отдельно от промпта. В `entries` английское имя и русские варианты;
не добавляй фабрикации вроде «термоформа — шаблон инфраструктуры».
Техническое «тэроформ-проект» — `Terraform-проект` при инфраструктурном контексте.
Автосбор идёт из принятых фонетически близких правок, сохраняет кандидатов;
активация требует минимум двух независимых сессий, буквального английского
написания в исходном ASR и отсутствия конфликта с другим именем. Повторный прогон
одной записи не является новым подтверждением. Автоматически добавленные пары
имеют `source=auto-evidence`, а не ручное подтверждение. Сомнительные пары оставляй
кандидатами; новые записи подхватываются новым worker-процессом.

### Проверка изменений

Не запускай реальные микрофон/колонки для проверки кода. Синтетический набор:

```sh
PYTHONPATH=backend ~/.local/share/assistant-stack/runtime/speech-venv/bin/python \
  -m unittest backend.test_speech_recognition backend.test_speech_glossary backend.test_timed_transcript \
  backend.test_recording_dataset_export backend.test_transcript_review \
  backend.test_recording_archive backend.test_acoustic_echo \
  backend.test_voice_cleanup backend.test_speech_dataset -q
```

Количество тестов проверяй по выводу текущего запуска. Не меняй требования/данные под зелёные тесты;
проверяй сохранность исходников, временные границы, разделение сессий и provenance.
