#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Распознавание голосовых локально через faster-whisper (данные не покидают сервер)."""

import threading

from .. import config

_whisper = None
_whisper_lock = threading.Lock()


def transcribe(file_path):
    """OGG/Opus -> текст. Модель грузится один раз и держится в памяти."""
    global _whisper
    from faster_whisper import WhisperModel
    with _whisper_lock:
        if _whisper is None:
            print("loading whisper model:", config.WHISPER_MODEL, flush=True)
            _whisper = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8",
                                    download_root=config.MODELS_DIR)
        segments, _info = _whisper.transcribe(file_path, language="ru", vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
