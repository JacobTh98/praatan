"""TextGrids nach IPA-Vokalen filtern, Formanten messen und pro Datei exportieren."""
from __future__ import annotations

# Hier die gewünschten Vokale auswählen. ae = æ; a ≠ ɑ und i ≠ ɪ.
# Diphthonge werden als ganze Laute gewählt, z. B. aɪ oder aʊ.
VOWELS = ('a', 'i', 'u', 'ae')
FILTER_MODE = 'ipa'  # 'ipa': exakte IPA-Vokale; 'letters': geschriebene Buchstaben
MAXIMUM_FORMANT = 5500.0  # Hz
TIME_STEP = 0.01         # Sekunden
WINDOW_LENGTH = 0.025    # Sekunden

import argparse
import logging
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter

from praatio import textgrid

from util import (AUDIO_SUFFIXES, SAMPLE_RATE, FilterResult, calculate_formants,
                  export_csv, filter_words, load_audio, parse_filters)

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def matching_audio(source: Path, audio_dir: Path) -> Path:
    name = source.stem if source.suffix.lower() == '.textgrid' else source.name
    matches = sorted(path for path in audio_dir.iterdir()
                     if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES and path.stem == name)
    if len(matches) != 1:
        raise ValueError(f'Für {source.name} genau eine gleichnamige Audiodatei erwartet, '
                         f'{len(matches)} gefunden in {audio_dir}.')
    return matches[0]


def is_textgrid(path: Path) -> bool:
    if not path.is_file() or path.name.startswith('.'):
        return False
    if not path.suffix or path.suffix.lower() == '.textgrid':
        return True
    # Generated filenames can contain periods without having a TextGrid suffix.
    with path.open('rb') as source:
        header = source.read(512)
    encoding = 'utf-16' if header.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    return '"TextGrid"' in header.decode(encoding, errors='replace')


def filter_file(source: Path, destination: Path, filters: tuple[str, ...],
                mode: str = 'letters', tier_name: str | None = None) -> FilterResult:
    if source.resolve() == destination.resolve():
        raise ValueError('Die Originaldatei darf nicht überschrieben werden.')
    logger.info('TextGrid lesen: %s', source)
    grid = textgrid.openTextgrid(str(source), includeEmptyIntervals=True, duplicateNamesMode='rename')
    tiers = [tier for tier in grid.tiers if isinstance(tier, textgrid.IntervalTier)]
    if tier_name is not None:
        tier = next((tier for tier in tiers if tier.name == tier_name), None)
    else:
        tier = next((tier for tier in tiers if tier.name.casefold() in {'word', 'words', 'wörter'}),
                    tiers[0] if tiers else None)
    if tier is None:
        raise ValueError(f'Keine passende Wort-Intervallebene gefunden: {tier_name or "automatische Auswahl"}')
    logger.info('Wortebene: %s | Modus: %s | Filter: %s', tier.name, mode, ', '.join(filters) or '(leer)')
    result = filter_words(list(tier.entries), filters, mode)
    if result.unknown:
        logger.warning('%d Wortvorkommen ohne verwendbare IPA: %s', len(result.unknown), ', '.join(sorted(set(result.unknown))))
    if result.unaligned_words:
        logger.warning('%d Wörter ohne gültige Einzelwortgrenzen ausgeschlossen.', result.unaligned_words)
    output = textgrid.Textgrid()
    # Keep the full original time axis, including an entirely empty result.
    for name, labels in ((tier.name, [word.label for word in result.words]),
                         (f'{tier.name} · IPA', [word.ipa for word in result.words])):
        output.addTier(textgrid.IntervalTier(name,
            [(word.start, word.end, label) for word, label in zip(result.words, labels)],
            minT=grid.minTimestamp, maxT=grid.maxTimestamp))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.filter-', dir=destination.parent) as directory:
        temporary = Path(directory) / 'result.TextGrid'
        output.save(str(temporary), format='long_textgrid', includeBlankSpaces=True,
                    minTimestamp=grid.minTimestamp, maxTimestamp=grid.maxTimestamp,
                    minimumIntervalLength=0)
        temporary.replace(destination)
    logger.info('Gespeichert: %s | insgesamt: %d | Filtertreffer: %d | übernommen mit IPA: %d',
                destination, result.total, result.matched, len(result.words))
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=ROOT / 'textgrids', help='Eingabeordner (Standard: textgrids neben dem Skript)')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'textgrids_filtered', help='Ausgabeordner (Standard: textgrids_filtered neben dem Skript)')
    parser.add_argument('--audio-dir', type=Path, default=ROOT / 'audios', help='Ordner mit gleichnamigen Audiodateien')
    parser.add_argument('--filter', default=', '.join(VOWELS), help='Überschreibt die VOWELS-Einstellung am Skriptanfang')
    parser.add_argument('--mode', choices=['letters', 'ipa'], default=FILTER_MODE, help='Überschreibt FILTER_MODE am Skriptanfang')
    parser.add_argument('--tier', help='Exakter Name der Wortebene; sonst Word/Words/Wörter oder erste Intervallebene')
    parser.add_argument('--overwrite', action='store_true', help='Vorhandene Ausgabedateien ersetzen')
    args = parser.parse_args(argv)
    started = perf_counter()
    try:
        filters = parse_filters(args.filter, args.mode)
        source_dir, output_dir = args.input_dir.resolve(), args.output_dir.resolve()
        if not source_dir.is_dir():
            raise ValueError(f'Eingabeordner fehlt: {source_dir}')
        if not args.audio_dir.is_dir():
            raise ValueError(f'Audioordner fehlt: {args.audio_dir}')
        if source_dir == output_dir or output_dir in source_dir.parents:
            raise ValueError('Der Ausgabeordner muss vom Eingabeordner getrennt sein und darf ihn nicht enthalten.')
        files = sorted(path for path in source_dir.iterdir() if is_textgrid(path))
        if not files:
            raise ValueError(f'Keine TextGrid-Dateien gefunden: {source_dir}')
    except (ValueError, OSError) as error:
        logger.error('%s', error)
        return 1
    logger.info('%d TextGrids gefunden. Ausgabe: %s', len(files), output_dir)
    logger.info('Vokalauswahl: %s | Modus: %s', ', '.join(filters) or '(leer)', args.mode)
    logger.info('IPA-Wörterbuchaussprache und F1–F4 werden für jede Datei exportiert. Messpunkt: Wortmitte als Näherung, keine Vokalgrenzen. frame = Sekunden; F1–F4 = Hz.')
    logger.info('Wort- und IPA-Ebene werden gespeichert. Nicht übernommene Wörter werden zu leeren Intervallen; Zeitgrenzen bleiben erhalten.')
    success = skipped = failed = total = matched = retained = 0
    for index, source in enumerate(files, 1):
        logger.info('Datei %d/%d: %s', index, len(files), source.name)
        destination = output_dir / source.name
        csv_destination = output_dir / 'formants' / f'{source.name}_formanten.csv'
        if (destination.exists() or csv_destination.exists()) and not args.overwrite:
            logger.warning('Mindestens eine Ausgabe existiert bereits, übersprungen: %s (für vollständige Neuberechnung --overwrite verwenden)', destination)
            skipped += 1
            continue
        try:
            audio_path = matching_audio(source, args.audio_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.analysis-', dir=output_dir) as directory:
                staged_grid = Path(directory) / 'result.TextGrid'
                result = filter_file(source, staged_grid, filters, args.mode, args.tier)
                rows = []
                if result.words:
                    samples = load_audio(audio_path)
                    rows = calculate_formants(samples, SAMPLE_RATE, result.words, audio_path.name,
                                              MAXIMUM_FORMANT, TIME_STEP, WINDOW_LENGTH)
                staged_csv = Path(directory) / 'result.csv'
                staged_csv.write_bytes(export_csv(rows))
                csv_destination.parent.mkdir(parents=True, exist_ok=True)
                staged_grid.replace(destination)
                staged_csv.replace(csv_destination)
            missing = sum(any(row[f'F{i}'] is None for i in range(1, 5)) for row in rows)
            logger.info('TextGrid: %s | CSV: %s | %d Messzeilen, %d mit fehlenden Formanten.',
                        destination, csv_destination, len(rows), missing)
        except Exception as error:
            if isinstance(error, subprocess.CalledProcessError) and error.stderr:
                logger.error('FFmpeg: %s', error.stderr.decode('utf-8', errors='replace'))
            logger.exception('Datei konnte nicht verarbeitet werden: %s', source)
            failed += 1
            continue
        success += 1
        total += result.total
        matched += result.matched
        retained += len(result.words)
    logger.info('Fertig nach %.1f s: %d gespeichert, %d übersprungen, %d fehlerhaft.',
                perf_counter() - started, success, skipped, failed)
    logger.info('Neu verarbeitete Dateien: %d Wörter insgesamt, %d Filtertreffer, %d mit IPA übernommen.', total, matched, retained)
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning('Filterung durch Benutzer abgebrochen.')
        raise SystemExit(130)
