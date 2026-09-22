import io
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
from praatio import textgrid

from textgrid_viewer import SAMPLE_RATE, audio_clip, make_figure, read_grid, waveform


class ViewerTests(unittest.TestCase):
    def test_textgrid_formats_and_unicode(self):
        grid = textgrid.Textgrid()
        grid.addTier(textgrid.IntervalTier('Wörter', [(0, 0.5, 'ä "ja"'), (0.5, 1, '')], 0, 1))
        grid.addTier(textgrid.PointTier('Punkte', [(0.25, 'Beginn')], 0, 1))
        with tempfile.TemporaryDirectory() as directory:
            for fmt in ('long_textgrid', 'short_textgrid'):
                path = Path(directory) / 'grid'
                grid.save(str(path), format=fmt, includeBlankSpaces=True)
                text = path.read_text(encoding='utf-8')
                for encoding in ('utf-8', 'utf-16'):
                    with self.subTest(format=fmt, encoding=encoding):
                        result = read_grid(text.encode(encoding))
                        self.assertEqual(result['tiers'][0]['entries'][0], (0.0, 0.5, 'ä "ja"'))
                        self.assertEqual(result['tiers'][0]['entries'][1][2], '')
                        self.assertTrue(result['tiers'][1]['point'])
                        self.assertEqual(result['tiers'][1]['entries'][0], (0.25, 0.25, 'Beginn'))

    def test_clip_contains_exact_samples(self):
        samples = np.arange(SAMPLE_RATE, dtype=np.int16)
        with wave.open(io.BytesIO(audio_clip(samples, 0.25, 0.75))) as clip:
            self.assertEqual(clip.getframerate(), SAMPLE_RATE)
            np.testing.assert_array_equal(np.frombuffer(clip.readframes(clip.getnframes()), dtype='<i2'),
                                          samples[round(0.25 * SAMPLE_RATE):round(0.75 * SAMPLE_RATE)])
        with self.assertRaises(ValueError):
            audio_clip(samples, 1, 1)

    def test_waveform_retains_short_peaks(self):
        samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
        samples[7], samples[8] = 32000, -31000
        x, y = waveform(samples, 0, 1)
        self.assertLessEqual(len(x), 3600)
        self.assertEqual(y.max(), 32000 / 32768)
        self.assertEqual(y.min(), -31000 / 32768)

    def test_chart_alignment_and_click_data(self):
        samples = np.zeros(SAMPLE_RATE, dtype=np.int16)
        tiers = [{'name': 'Words', 'point': False, 'entries': [(0.2, 0.5, '<hello>')]},
                 {'name': 'Points', 'point': True, 'entries': [(0.3, 0.3, 'x')]}]
        chart = make_figure(samples, tiers, 0.1, 0.6)
        self.assertEqual(tuple(chart.layout.xaxis.range), (0.1, 0.6))
        self.assertEqual(tuple(chart.layout.xaxis3.range), (0.1, 0.6))
        self.assertEqual(chart.data[1].customdata[0][:2], [0.2, 0.5])
        self.assertEqual(chart.data[1].text[0], '&lt;hello&gt;')


if __name__ == '__main__':
    unittest.main()
