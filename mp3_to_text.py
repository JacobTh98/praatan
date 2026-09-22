"""Transcribe MP3 files into Praat TextGrid files.

Install the transcription engine before running this script:
        pip install openai-whisper
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import subprocess
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import perf_counter
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@contextmanager
def report_step(description: str) -> Iterator[None]:
    """Report elapsed time even while Whisper is busy without new output."""
    started = perf_counter()
    finished = Event()
    logger.info("%s …", description)

    def report_wait() -> None:
        while not finished.wait(15):
            logger.info(
                "%s: läuft noch (%.0f Sekunden vergangen).",
                description,
                perf_counter() - started,
            )

    thread = Thread(target=report_wait, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        logger.error(
            "%s: abgebrochen nach %.1f Sekunden.", description, perf_counter() - started
        )
        raise
    else:
        logger.info(
            "%s: abgeschlossen nach %.1f Sekunden.",
            description,
            perf_counter() - started,
        )
    finally:
        finished.set()
        thread.join()


def audio_duration(audio_path: Path) -> float:
    """Return the audio duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def escape_textgrid_text(text: str) -> str:
    return text.replace('"', '""')


def words_from_result(result: dict[str, Any]) -> list[tuple[float, float, str]]:
    words: list[tuple[float, float, str]] = []
    for segment in result.get("segments", []):
        segment_words = segment.get("words", [])
        if segment_words:
            for word in segment_words:
                text = word.get("word", "").strip()
                if text and word.get("end", 0) > word.get("start", 0):
                    words.append((float(word["start"]), float(word["end"]), text))
        else:
            text = segment.get("text", "").strip()
            if text and segment["end"] > segment["start"]:
                words.append((float(segment["start"]), float(segment["end"]), text))
    return words


def write_textgrid(
    output_path: Path, duration: float, words: list[tuple[float, float, str]]
) -> None:
    intervals: list[tuple[float, float, str]] = []
    position = 0.0
    for start, end, text in words:
        start = max(0.0, min(start, duration))
        end = max(start, min(end, duration))
        if start > position:
            intervals.append((position, start, ""))
        if end > start:
            intervals.append((start, end, text))
            position = end
    if position < duration:
        intervals.append((position, duration, ""))
    if not intervals:
        intervals.append((0.0, duration, ""))

    lines = [
        'File type = "ooTextFile"',
        'Object class = "TextGrid"',
        "",
        "xmin = 0",
        f"xmax = {duration}",
        "tiers? <exists>",
        "size = 1",
        "item []:",
        "    item [1]:",
        '        class = "IntervalTier"',
        '        name = "Word"',
        "        xmin = 0",
        f"        xmax = {duration}",
        f"        intervals: size = {len(intervals)}",
    ]
    for index, (start, end, text) in enumerate(intervals, start=1):
        lines.extend(
            [
                f"        intervals [{index}]:",
                f"            xmin = {start}",
                f"            xmax = {end}",
                f'            text = "{escape_textgrid_text(text)}"',
            ]
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def transcribe_directory(
    audio_dir: Path, output_dir: Path, model_name: str, language: str | None
) -> None:
    started = perf_counter()
    logger.info("Transkription gestartet. Eingabe: %s", audio_dir.resolve())
    logger.info("Ausgabe: %s", output_dir.resolve())
    logger.info(
        "Whisper-Modell: %s | Sprache: %s | Wortzeitstempel: aktiviert",
        model_name,
        language or "automatische Erkennung",
    )
    logger.info("Suche MP3-Dateien direkt im Eingabeordner (Suchmuster: *.mp3).")
    if not audio_dir.is_dir():
        raise SystemExit(
            f"Eingabeordner existiert nicht oder ist kein Ordner: {audio_dir}"
        )
    audio_files = sorted(audio_dir.glob("*.mp3"))
    if not audio_files:
        raise SystemExit(f"Keine MP3-Dateien in {audio_dir} gefunden.")
    logger.info("%d MP3-Datei(en) gefunden.", len(audio_files))
    try:
        with report_step("Whisper und seine Abhängigkeiten laden"):
            whisper = importlib.import_module("whisper")
    except ImportError as error:
        raise SystemExit(
            "Whisper fehlt. Installiere es mit: pip install openai-whisper"
        ) from error

    logger.info(
        "Falls das Modell lokal fehlt, lädt Whisper es herunter. Das Laden kann einige Zeit dauern."
    )
    with report_step(f"Modell '{model_name}' laden"):
        model = whisper.load_model(model_name)
    logger.info("Verwendetes Rechengerät: %s", model.device)
    logger.info("Ausgabeordner bereitstellen: %s", output_dir.resolve())
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, audio_path in enumerate(audio_files, start=1):
        file_started = perf_counter()
        logger.info("Datei %d/%d: %s", index, len(audio_files), audio_path.name)
        with report_step(f"Audiodauer mit ffprobe ermitteln: {audio_path.name}"):
            duration = audio_duration(audio_path)
        logger.info(
            "Audiodauer: %.2f Sekunden (%.2f Minuten).", duration, duration / 60
        )
        options: dict[str, Any] = {"word_timestamps": True, "verbose": True}
        if language:
            options["language"] = language
        logger.info(
            "Whisper gibt erkannte Textabschnitte mit Zeitstempeln aus; die Wortausrichtung kann anschließend weiterlaufen."
        )
        with report_step(
            f"Audio transkribieren und Wortzeitstempel bestimmen: {audio_path.name}"
        ):
            result = model.transcribe(str(audio_path), **options)
        logger.info(
            "Sprache laut Whisper: %s | Textabschnitte: %d",
            result.get("language", "unbekannt"),
            len(result.get("segments", [])),
        )
        logger.info("Wortintervalle aus dem Transkriptionsergebnis aufbereiten.")
        words = words_from_result(result)
        logger.info(
            "%d beschriftete Intervalle aufbereitet (ohne Wortzeitstempel werden Textabschnitte verwendet).",
            len(words),
        )
        if not words:
            logger.warning(
                "Keine verwendbaren Textintervalle erkannt. Die TextGrid-Datei enthält nur ein leeres Intervall."
            )
        output_path = output_dir / audio_path.with_suffix("").name
        if output_path.exists():
            logger.warning(
                "Vorhandene Ausgabedatei wird überschrieben: %s", output_path.resolve()
            )
        with report_step(
            "Praat-TextGrid mit Wortintervallen und leeren Pausenintervallen speichern"
        ):
            write_textgrid(output_path, duration, words)
        logger.info(
            "Gespeichert: %s | Dateigröße: %d Bytes",
            output_path.resolve(),
            output_path.stat().st_size,
        )
        logger.info(
            "Datei %d/%d fertig nach %.1f Sekunden.",
            index,
            len(audio_files),
            perf_counter() - file_started,
        )
    logger.info(
        "Alle %d Datei(en) erfolgreich verarbeitet. Gesamtdauer: %.1f Sekunden.",
        len(audio_files),
        perf_counter() - started,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-dir", type=Path, default=Path("audios"))
    parser.add_argument("--output-dir", type=Path, default=Path("textgrids"))
    parser.add_argument(
        "--model",
        default="small",
        help="Whisper-Modell, z. B. tiny, small oder medium, default: small",
    )
    parser.add_argument(
        "--language", default="en", help="Sprache, z. B. en oder de, default: en"
    )
    args = parser.parse_args()
    try:
        transcribe_directory(args.audio_dir, args.output_dir, args.model, args.language)
    except KeyboardInterrupt:
        logger.warning("Transkription durch Benutzer abgebrochen.")
        raise SystemExit(130)
    except SystemExit as error:
        logger.error("%s", error)
        raise SystemExit(1) from error
    except Exception as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            logger.error("Ausgabe des externen Programms: %s", error.stderr.strip())
        if isinstance(error, FileNotFoundError) and error.filename in {
            "ffprobe",
            "ffmpeg",
        }:
            logger.error(
                "%s fehlt. Installiere FFmpeg inklusive ffprobe und stelle sicher, dass beide im PATH liegen.",
                error.filename,
            )
        logger.exception("Transkription fehlgeschlagen: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
