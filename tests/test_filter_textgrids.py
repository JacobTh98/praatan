import tempfile
import unittest
import csv
import wave
from pathlib import Path

from praatio import textgrid

from filter_textgrids import filter_file, is_textgrid, main, matching_audio
from util import filter_words, parse_filters


class BatchFilterTests(unittest.TestCase):
    def make_grid(self, path):
        grid = textgrid.Textgrid()
        grid.addTier(textgrid.IntervalTier("Other", [(0, 4, "cat dog see")], 0, 4))
        grid.addTier(
            textgrid.IntervalTier(
                "Word", [(0.1, 0.5, "cat"), (1, 1.5, "dog"), (2, 2.5, "see")], 0, 4
            )
        )
        grid.save(str(path), format="long_textgrid", includeBlankSpaces=True)

    def test_same_filter_as_viewer_and_preserved_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            source, dest = Path(folder) / "song.v1", Path(folder) / "out"
            self.make_grid(source)
            self.assertTrue(is_textgrid(source))
            for mode, value in [
                ("letters", "a, i,u, ae"),
                ("ipa", "i, ae"),
                ("letters", ""),
            ]:
                with self.subTest(mode=mode, value=value):
                    filters = parse_filters(value, mode)
                    result = filter_file(source, dest, filters, mode)
                    original = textgrid.openTextgrid(
                        str(source), includeEmptyIntervals=True
                    )
                    expected = filter_words(
                        list(original.getTier("Word").entries), filters, mode
                    )
                    self.assertEqual(result, expected)
                    output = textgrid.openTextgrid(
                        str(dest), includeEmptyIntervals=True
                    )
                    self.assertEqual(output.tierNames, ("Word", "Word · IPA"))
                    self.assertEqual((output.minTimestamp, output.maxTimestamp), (0, 4))
                    self.assertEqual(
                        [tuple(e) for e in output.getTier("Word").entries if e.label],
                        [(w.start, w.end, w.label) for w in expected.words],
                    )
                    self.assertEqual(
                        [
                            e.label
                            for e in output.getTier("Word · IPA").entries
                            if e.label
                        ],
                        [w.ipa for w in expected.words],
                    )

    def test_batch_continues_after_error_and_protects_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            source, dest = Path(folder) / "input", Path(folder) / "output"
            source.mkdir()
            self.make_grid(source / "good")
            audio_dir = Path(folder) / "audios"
            audio_dir.mkdir()
            with wave.open(str(audio_dir / "good.wav"), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(22050)
                audio.writeframes(b"\x00\x00" * 22050 * 4)
            (source / "broken.TextGrid").write_text("invalid")
            args = [
                "--input-dir",
                str(source),
                "--output-dir",
                str(dest),
                "--audio-dir",
                str(audio_dir),
            ]
            self.assertEqual(main(args), 1)
            self.assertTrue((dest / "good").exists())
            with (dest / "formants" / "good_formanten.csv").open(
                encoding="utf-8-sig"
            ) as csv_file:
                rows = list(csv.DictReader(csv_file))
            self.assertEqual([row["vowel"] for row in rows], ["æ", "i"])
            self.assertEqual([row["frame"] for row in rows], ["0.3", "2.25"])
            self.assertTrue(all(row["datei"] == "good.wav" for row in rows))
            self.assertTrue(all(row["F1"] == "" for row in rows))
            (dest / "good").write_text("existing output")
            self.assertEqual(main(args), 1)
            self.assertEqual((dest / "good").read_text(), "existing output")
            self.assertEqual(main(args + ["--overwrite"]), 1)
            self.assertIn("TextGrid", (dest / "good").read_text())
            self.assertEqual(
                main(["--input-dir", str(source), "--output-dir", str(source)]), 1
            )
            self.assertEqual(
                main(args + ["--mode", "ipa", "--filter", "not-a-vowel"]), 1
            )

    def test_missing_or_ambiguous_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "song.TextGrid"
            with self.assertRaises(ValueError):
                matching_audio(source, root)
            (root / "song.wav").touch()
            self.assertEqual(matching_audio(source, root), root / "song.wav")
            (root / "song.mp3").touch()
            with self.assertRaises(ValueError):
                matching_audio(source, root)


if __name__ == "__main__":
    unittest.main()
