"""Word filtering, English IPA and Praat/Burg formant measurements."""
from __future__ import annotations

import csv
import io
import logging
import math
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)
EXPORT_COLUMNS = ('datei', 'vowel', 'frame', 'wordcontext', 'F1', 'F2', 'F3', 'F4')
SAMPLE_RATE = 22050
AUDIO_SUFFIXES = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac'}


def load_audio(path: Path) -> np.ndarray:
    """Decode the same mono PCM signal used by the Streamlit viewer."""
    logger.info('Audio laden und nach Mono mit %d Hz dekodieren: %s', SAMPLE_RATE, path)
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-vn', '-ac', '1',
         '-ar', str(SAMPLE_RATE), '-f', 's16le', 'pipe:1'],
        capture_output=True, check=True, timeout=180,
    )
    samples = np.frombuffer(result.stdout, dtype='<i2').copy()
    if not samples.size:
        raise ValueError(f'Keine Audiosamples in {path}')
    logger.info('Audio geladen: %.2f Sekunden.', len(samples) / SAMPLE_RATE)
    return samples
WORD_PATTERN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
# Longest tokens first: /i/ must not match /ɪ/ or the second part of /aɪ/.
VOWEL_PATTERN = re.compile(r'aɪ|aʊ|eɪ|oʊ|ɔɪ|ɪə|eə|ʊə|[aeiouyɑɒæɛɜɞɐəɚɝɪʊɔʌøœɶɨʉɯɤɘɵ](?:ː|ˑ)?')


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    label: str
    ipa: str
    vowels: tuple[str, ...]
    context: str


@dataclass(frozen=True)
class FilterResult:
    total: int
    matched: int
    words: tuple[Word, ...]
    unknown: tuple[str, ...]
    unaligned_words: int


def parse_filters(value: str, mode: str = 'letters') -> tuple[str, ...]:
    if mode not in {'letters', 'ipa'}:
        raise ValueError('Unbekannter Filtermodus.')
    tokens = [token.strip().casefold() for token in value.split(',') if token.strip()]
    if mode == 'ipa':
        tokens = ['æ' if token == 'ae' else token for token in tokens]
        if any(VOWEL_PATTERN.fullmatch(token) is None for token in tokens):
            raise ValueError('Bitte einzelne IPA-Vokale kommagetrennt eingeben, z. B. æ, i, u, ɑ, aɪ. ae steht für æ.')
    elif any(not token.isalpha() for token in tokens):
        raise ValueError('Bitte nur Buchstaben oder Buchstabenfolgen kommagetrennt eingeben.')
    return tuple(dict.fromkeys(tokens))


@lru_cache(maxsize=20000)
def english_ipa(word: str) -> str | None:
    import eng_to_ipa

    transcription = eng_to_ipa.convert(word.casefold().replace('’', "'"), keep_punct=False)
    # Asterisks mark dictionary misses, not phonetic transcriptions.
    return None if '*' in transcription or not transcription.strip() else transcription


def filter_words(entries: list[tuple[float, float, str]], filters: tuple[str, ...],
                 mode: str = 'letters', transcribe: Callable[[str], str | None] = english_ipa) -> FilterResult:
    """Count occurrences, not distinct spellings. Never invent sub-word boundaries."""
    if mode not in {'letters', 'ipa'}:
        raise ValueError('Unbekannter Filtermodus.')
    tokenized = [(a, b, label, WORD_PATTERN.findall(label)) for a, b, label in entries]
    context_entries = [entry for entry in tokenized if entry[3]]
    total = sum(len(tokens) for _, _, _, tokens in context_entries)
    words, unknown = [], []
    matched = unaligned = 0
    for index, (start, end, label, tokens) in enumerate(context_entries):
        if len(tokens) != 1 or not math.isfinite(start) or not math.isfinite(end) or end <= start:
            unaligned += len(tokens)
            continue
        word = tokens[0]
        if not filters or (mode == 'letters' and not any(token in word.casefold() for token in filters)):
            continue
        ipa = transcribe(word)
        if mode == 'letters':
            matched += 1
        if ipa is None:
            unknown.append(word)
            continue
        vowels = tuple(VOWEL_PATTERN.findall(ipa))
        if mode == 'ipa':
            vowels = tuple(vowel for vowel in vowels if vowel in filters)
            if not vowels:
                continue
            matched += 1
        if not vowels:
            unknown.append(word)
            continue
        context = ' '.join(f'[{context_entries[pos][2]}]' if pos == index else context_entries[pos][2]
                           for pos in range(max(0, index - 1), min(len(context_entries), index + 2)))
        words.append(Word(start, end, label, ipa, vowels, context))
    logger.info('Wortfilter: %d insgesamt, %d Treffer, %d mit IPA auswertbar, %d ohne Einzelwortgrenzen.',
                total, matched, len(words), unaligned)
    return FilterResult(total, matched, tuple(words), tuple(unknown), unaligned)


def calculate_formants(samples: np.ndarray, sample_rate: int, words: tuple[Word, ...], filename: str,
                       maximum_formant: float = 5500.0, time_step: float = 0.01,
                       window_length: float = 0.025) -> list[dict]:
    """One row per word midpoint, NOT per aligned vowel. frame is seconds.

    vowel lists the word's IPA vowels (only matching vowels in IPA-filter mode).
    Missing estimates remain empty; no silent zero substitution or extrapolation.
    """
    import parselmouth

    if sample_rate <= 0 or not 0 < maximum_formant < sample_rate / 2:
        raise ValueError('Die Formantengrenze muss unter der halben Abtastrate liegen.')
    if not math.isfinite(time_step) or not math.isfinite(window_length) or time_step <= 0 or window_length <= 0:
        raise ValueError('Zeitschritt und Fensterlänge müssen positiv und endlich sein.')
    if samples.ndim != 1 or not samples.size:
        raise ValueError('Die Formantenanalyse benötigt ein nicht leeres Mono-Audiosignal.')
    if not words:
        return []
    signal = samples.astype(np.float64)
    if np.issubdtype(samples.dtype, np.integer):
        signal /= max(abs(np.iinfo(samples.dtype).min), np.iinfo(samples.dtype).max)
    if not np.isfinite(signal).all():
        raise ValueError('Das Audiosignal enthält ungültige Werte.')
    duration = len(signal) / sample_rate
    logger.info('Burg-Analyse starten: %.2f s Audio, %d Wörter, Obergrenze %.0f Hz.', duration, len(words), maximum_formant)
    formants = None
    if duration >= 2 * window_length:
        formants = parselmouth.Sound(signal, sampling_frequency=sample_rate).to_formant_burg(
            time_step=time_step, max_number_of_formants=5.0,
            maximum_formant=maximum_formant, window_length=window_length, pre_emphasis_from=50.0)
    frame_times = formants.xs() if formants is not None else np.array([])
    rows = []
    for word in words:
        time = (word.start + word.end) / 2
        values = [None] * 4
        if formants is not None and 0 <= word.start < word.end <= duration:
            # Do not extrapolate a measurement from beyond the available frame centres.
            if frame_times[0] <= time <= frame_times[-1]:
                for number in range(1, 5):
                    value = formants.get_value_at_time(number, time)
                    if math.isfinite(value) and value > 0:
                        values[number - 1] = round(value, 2)
        rows.append(dict(zip(EXPORT_COLUMNS, [filename, ' '.join(word.vowels), time, word.context, *values])))
    logger.info('Formantenanalyse fertig: %d Zeilen, %d mit vollständigen F1–F4.', len(rows),
                sum(all(row[f'F{n}'] is not None for n in range(1, 5)) for row in rows))
    return rows


def export_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8-sig')
