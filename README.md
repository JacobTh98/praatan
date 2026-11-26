# praatan

This software accompanies Annemarie Simon’s examination thesis *Language and Gender: A Phonetic Analysis of the Transitioning Process*.

Whisper transcribes English recordings into word-level Praat TextGrids. The analysis selects words whose dictionary pronunciation contains a target IPA vowel. It estimates the first four formants (F1–F4, in Hz) at each selected word’s midpoint, which can fall on a consonant.

## Quick start

Install FFmpeg (`ffmpeg` and `ffprobe`), place your MP3 files in `audios/`, and run these commands from the repository root.

```bash
python -m pip install -r requirements-viewer.txt openai-whisper
python mp3_to_text.py
python filter_textgrids.py
```

If you already have word-level TextGrids, place them in `textgrids/` with the same base names as the recordings. You can then skip Whisper installation and transcription.

Find filtered TextGrids in `textgrids_filtered/` and one formant CSV per recording in `textgrids_filtered/formants/`. To inspect a recording interactively, run `python -m streamlit run textgrid_viewer.py`.

To cite the software, see [CITATION.cff](CITATION.cff).
