import csv
import io
import unittest
from unittest.mock import patch

import numpy as np
import parselmouth

from util import EXPORT_COLUMNS, Word, calculate_formants, english_ipa, export_csv, filter_words, parse_filters


class AnalysisTests(unittest.TestCase):
    def test_letter_filter_counts_occurrences_and_converts_only_matches(self):
        entries = [(0, 1, 'CAT,'), (1, 2, 'dog'), (2, 3, 'cat'), (3, 4, ''), (4, 5, 'unknown')]
        with patch('util.english_ipa') as unused:
            seen = []
            def convert(word):
                seen.append(word.casefold())
                return 'kæt' if word.casefold() == 'cat' else None
            result = filter_words(entries, parse_filters('a, i,u, ae'), transcribe=convert)
        self.assertEqual(result.total, 4)
        self.assertEqual(result.matched, 3)
        self.assertEqual(len(result.words), 2)
        self.assertEqual(seen, ['cat', 'cat', 'unknown'])
        self.assertEqual(result.words[0].context, '[CAT,] dog')
        self.assertEqual(result.unknown, ('unknown',))

    def test_exact_ipa_symbols_not_substrings(self):
        entries = [(0, 1, 'eye'), (1, 2, 'see'), (2, 3, 'sit'), (3, 4, 'cat')]
        ipa = {'eye': 'aɪ', 'see': 'si', 'sit': 'sɪt', 'cat': 'kæt'}
        result = filter_words(entries, parse_filters('i, ae', 'ipa'), 'ipa', ipa.get)
        self.assertEqual([word.label for word in result.words], ['see', 'cat'])
        self.assertEqual([word.vowels for word in result.words], [('i',), ('æ',)])
        self.assertFalse(filter_words(entries, (), transcribe=ipa.get).words)

    def test_sentence_intervals_do_not_get_fabricated_word_timings(self):
        result = filter_words([(0, 1, 'a cat'), (1, 1, 'cat')], ('a',), transcribe=lambda _: 'kæt')
        self.assertEqual(result.total, 3)
        self.assertEqual(result.unaligned_words, 3)
        self.assertFalse(result.words)

    def test_english_dictionary_and_unknown(self):
        self.assertIn('æ', english_ipa('cat'))
        self.assertIsNone(english_ipa('zzzznonexistentwordzzzz'))

    def test_formants_match_praat_at_word_midpoint_and_export(self):
        rate = 22050
        # Deterministic broadband signal tests exact time/unit/normalization plumbing.
        samples = np.random.default_rng(42).integers(-15000, 15000, rate, dtype=np.int16)
        word = Word(0.2, 0.6, 'cat', 'kæt', ('æ',), 'a [cat] here')
        rows = calculate_formants(samples, rate, (word,), 'song.mp3')
        reference = parselmouth.Sound(samples.astype(float) / 32768, sampling_frequency=rate).to_formant_burg(
            time_step=0.01, max_number_of_formants=5, maximum_formant=5500,
            window_length=0.025, pre_emphasis_from=50)
        self.assertEqual(rows[0]['frame'], 0.4)
        for number in range(1, 5):
            self.assertEqual(rows[0][f'F{number}'], round(reference.get_value_at_time(number, 0.4), 2))
        parsed = list(csv.DictReader(io.StringIO(export_csv(rows).decode('utf-8-sig'))))
        self.assertEqual(tuple(parsed[0]), EXPORT_COLUMNS)
        self.assertEqual(parsed[0]['vowel'], 'æ')
        self.assertEqual(parsed[0]['wordcontext'], 'a [cat] here')

    def test_silence_out_of_range_and_short_audio_preserve_missing_values(self):
        for samples, word in [(np.zeros(22050, dtype=np.int16), Word(0.2, 0.6, 'cat', 'kæt', ('æ',), '[cat]')),
                              (np.ones(22050, dtype=np.int16), Word(2, 3, 'cat', 'kæt', ('æ',), '[cat]')),
                              (np.zeros(100, dtype=np.int16), Word(0, 0.003, 'cat', 'kæt', ('æ',), '[cat]'))]:
            rows = calculate_formants(samples, 22050, (word,), 'song.mp3')
            self.assertTrue(all(rows[0][f'F{i}'] is None for i in range(1, 5)))
            self.assertNotIn('nan', export_csv(rows).decode())
        self.assertEqual(calculate_formants(np.zeros(100), 22050, (), 'song.mp3'), [])


if __name__ == '__main__':
    unittest.main()
