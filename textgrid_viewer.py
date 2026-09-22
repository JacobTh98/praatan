"""Start with: streamlit run textgrid_viewer.py."""
from __future__ import annotations

import hashlib
import html
import io
import logging
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from praatio import textgrid
from util import calculate_formants, export_csv, filter_words, parse_filters

ROOT = Path(__file__).resolve().parent
SAMPLE_RATE = 22050
AUDIO_SUFFIXES = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac'}
logger = logging.getLogger(__name__)
cached_filter_words = st.cache_data(max_entries=16, show_spinner=False)(filter_words)
cached_formants = st.cache_data(max_entries=2, show_spinner=False)(calculate_formants)


def word_analysis(grid: dict, samples: np.ndarray, name: str, identity: str) -> tuple[list[dict], str]:
    """Apply a single word filter to the viewer, IPA table and formant export."""
    interval_tiers = [tier for tier in grid['tiers'] if not tier['point']]
    if not interval_tiers:
        st.info('Für die Wortanalyse wird eine Intervallebene mit Einzelwortgrenzen benötigt.')
        return [], identity
    names = [tier['name'] for tier in interval_tiers]
    default = next((i for i, title in enumerate(names) if title.casefold() in {'word', 'words', 'wörter'}), 0)
    tier_name = st.sidebar.selectbox('Wortebene für die Analyse', names, index=default, key=f'word-tier-{identity}')
    tier = next(tier for tier in interval_tiers if tier['name'] == tier_name)
    value = st.sidebar.text_input('Buchstaben / Vokale (kommagetrennt)', value='a, i, u, ae', key='vowel-filter')
    mode_label = st.sidebar.radio('Filter anwenden auf', ['Geschriebene Buchstaben', 'Exakte IPA-Vokale'])
    mode = 'letters' if mode_label == 'Geschriebene Buchstaben' else 'ipa'
    st.sidebar.caption('Buchstaben: enthält mindestens eine eingegebene Folge, unabhängig von Groß-/Kleinschreibung. IPA: exakte Vokale; ae = æ, i ≠ ɪ, a ≠ ɑ oder aɪ.')
    try:
        filters = parse_filters(value, mode)
        with st.spinner('Wörter filtern und englische IPA-Aussprache nachschlagen …'):
            result = cached_filter_words(tier['entries'], filters, mode)
    except ValueError as error:
        st.error(str(error))
        return [], identity
    except ImportError:
        st.error('Die IPA-Bibliothek fehlt. Bitte ausführen: python -m pip install -r requirements-viewer.txt')
        return [], identity
    analysis_id = hashlib.sha256(f'{identity}-{tier_name}-{mode}-{filters}'.encode()).hexdigest()[:16]
    total, matched, usable = st.columns(3)
    total.metric('Wörter insgesamt', result.total)
    matched.metric('Gefilterte Wörter', result.matched)
    usable.metric('Mit IPA auswertbar', len(result.words))
    st.caption('Gezählt werden Wortvorkommen in der gewählten Ebene der gesamten Datei, nicht nur im sichtbaren Ausschnitt. Ab hier werden nur Filtertreffer mit bekannter IPA-Aussprache verwendet.')
    if result.unaligned_words:
        st.warning(f'{result.unaligned_words} Wörter haben keine eigenen gültigen Wortgrenzen und werden nicht analysiert.')
    if result.unknown:
        st.warning(f'{len(result.unknown)} Wortvorkommen ohne verwendbare IPA-Aussprache; sie werden nicht gemessen.')
        with st.expander('Wörter ohne verwendbare IPA-Aussprache'):
            st.write(', '.join(sorted(set(result.unknown))))
    if not result.words:
        st.info('Keine auswertbaren Treffer. Bitte den Filter ändern. Ein leeres Filterfeld wählt keine Wörter aus.')
        return [], analysis_id
    with st.expander('Gefilterte Wörter und englische IPA-Aussprache'):
        st.dataframe([{'Wort': word.label, 'IPA': word.ipa, 'Vokale': ' '.join(word.vowels),
                       'Start (s)': word.start, 'Ende (s)': word.end} for word in result.words],
                     width='stretch', hide_index=True)
        st.caption('Wörterbuchaussprache (US-Englisch), keine Lauterkennung aus dem Audio. Bei mehreren Aussprachen wird eine Wörterbuchvariante verwendet.')
    st.markdown('**Formanten F1–F4 für diese Datei**')
    st.info('Messung in der Wortmitte als Näherung: Die TextGrids enthalten keine Vokalgrenzen. '
            'Das ist keine Messung eines zeitlich ausgerichteten einzelnen Vokals. '
            'Pro Wort gibt es eine Zeile; „vowel“ listet dessen IPA-Vokale (im IPA-Modus nur passende Vokale).')
    with st.expander('Einstellungen der Formantenanalyse'):
        ceiling = st.number_input('Formantenobergrenze (Hz)', min_value=3000.0, max_value=10000.0,
                                  value=5500.0, step=100.0)
        step = st.number_input('Analyseschritt (s)', min_value=0.001, max_value=0.1, value=0.01, step=0.001, format='%.3f')
        window = st.number_input('Fensterlänge (s)', min_value=0.005, max_value=0.1, value=0.025, step=0.005, format='%.3f')
        st.caption('Praat/Burg mit maximal 5 Formanten und Präemphase ab 50 Hz. Die Obergrenze muss zur Stimme passen. Musik, Störgeräusche oder Konsonanten in der Wortmitte können die Schätzungen verfälschen.')
    calculation_id = f'{analysis_id}-{ceiling}-{step}-{window}'
    if st.button('F1–F4 für alle gefilterten Wörter berechnen', key=f'calculate-{analysis_id}'):
        try:
            with st.spinner(f'Formanten für {len(result.words)} Wörter berechnen …'):
                rows = cached_formants(samples, SAMPLE_RATE, result.words, name, ceiling, step, window)
            st.session_state['formant-result'] = (calculation_id, rows)
        except ImportError:
            st.error('Praat-Parselmouth fehlt. Bitte ausführen: python -m pip install -r requirements-viewer.txt')
        except Exception as error:
            logger.exception('Formantenanalyse fehlgeschlagen.')
            st.error(f'Formanten konnten nicht berechnet werden: {error}')
    saved_id, rows = st.session_state.get('formant-result', (None, []))
    if saved_id == calculation_id:
        missing = sum(any(row[f'F{number}'] is None for number in range(1, 5)) for row in rows)
        st.caption(f'{len(rows)} Messzeilen · {missing} mit fehlenden Werten. frame = Wortmitte in Sekunden ab Audiobeginn; F1–F4 in Hz. '
                   'wordcontext enthält das Zielwort in eckigen Klammern mit Nachbarwörtern. Fehlende Schätzungen bleiben im CSV leer.')
        st.dataframe(rows, width='stretch', hide_index=True)
        st.download_button('Formantentabelle als CSV exportieren', data=export_csv(rows),
                           file_name=f'{Path(name).stem}_formanten.csv', mime='text/csv', key=f'export-{calculation_id}')
    filtered_tier = {'name': tier_name, 'point': False,
                     'entries': [(word.start, word.end, word.label) for word in result.words]}
    ipa_tier = {'name': f'{tier_name} · IPA', 'point': False,
                'entries': [(word.start, word.end, word.ipa) for word in result.words]}
    return [filtered_tier, ipa_tier], analysis_id


@st.cache_data(max_entries=4, show_spinner=False)
def read_grid(data: bytes) -> dict:
    """Let praatio handle long/short TextGrids and UTF-8/UTF-16 encoding."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'input.TextGrid'
        path.write_bytes(data)
        grid = textgrid.openTextgrid(str(path), includeEmptyIntervals=True, duplicateNamesMode='rename')
    tiers = []
    for tier in grid.tiers:
        is_point = isinstance(tier, textgrid.PointTier)
        entries = [
            (float(entry[0]), float(entry[0] if is_point else entry[1]), str(entry[-1]))
            for entry in tier.entries
        ]
        tiers.append({'name': tier.name, 'point': is_point, 'entries': entries})
    if not tiers:
        raise ValueError('Die TextGrid-Datei enthält keine Annotationsebenen.')
    return {'start': grid.minTimestamp, 'end': grid.maxTimestamp, 'tiers': tiers}


@st.cache_data(max_entries=2, show_spinner=False)
def decode_audio(data: bytes) -> np.ndarray:
    """Decode a seekable local file, including formats needing end-of-file metadata."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'audio'
        path.write_bytes(data)
        logger.info('Audio für die Wellenform dekodieren (%d Bytes).', len(data))
        result = subprocess.run(
            ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-vn',
             '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 's16le', 'pipe:1'],
            capture_output=True, check=True, timeout=180,
        )
    samples = np.frombuffer(result.stdout, dtype='<i2').copy()
    if not samples.size:
        raise ValueError('Die Audiodatei enthält keine dekodierbaren Audiosamples.')
    logger.info('Audio geladen: %.2f Sekunden.', len(samples) / SAMPLE_RATE)
    return samples


def audio_clip(samples: np.ndarray, start: float, end: float) -> bytes:
    first = max(0, round(start * SAMPLE_RATE))
    last = min(len(samples), round(end * SAMPLE_RATE))
    if last <= first:
        raise ValueError('Der ausgewählte Bereich enthält keine Audiosamples.')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples[first:last].astype('<i2', copy=False).tobytes())
    return buffer.getvalue()


def waveform(samples: np.ndarray, start: float, end: float) -> tuple[np.ndarray, np.ndarray]:
    """Preserve peaks instead of skipping samples when zooming out."""
    first = max(0, int(start * SAMPLE_RATE))
    last = min(len(samples), int(np.ceil(end * SAMPLE_RATE)))
    chunk = samples[first:last]
    if not chunk.size:
        return np.array([]), np.array([])
    stride = max(1, int(np.ceil(len(chunk) / 1800)))
    offsets = np.arange(0, len(chunk), stride)
    low = np.minimum.reduceat(chunk, offsets) / 32768.0
    high = np.maximum.reduceat(chunk, offsets) / 32768.0
    return np.repeat((first + offsets) / SAMPLE_RATE, 2), np.column_stack((low, high)).ravel()


def make_figure(samples: np.ndarray, tiers: list[dict], start: float, end: float) -> go.Figure:
    figure = make_subplots(rows=len(tiers) + 1, cols=1, shared_xaxes=True,
                          row_heights=[3] + [1] * len(tiers), vertical_spacing=0.025)
    x, y = waveform(samples, start, end)
    figure.add_trace(go.Scatter(x=x, y=y, mode='lines', line={'color': '#294e68', 'width': 1},
                               hoverinfo='skip', name='Audio'), row=1, col=1)
    figure.update_yaxes(title_text='Amplitude', range=[-1, 1], row=1, col=1)
    for row, tier in enumerate(tiers, start=2):
        visible = [(a, b, label) for a, b, label in tier['entries'] if b >= start and a <= end]
        # Detailed borders are only useful at close zoom; retain a bounded chart size.
        detailed = len(visible) <= 250
        if detailed:
            for a, b, label in visible:
                figure.add_shape(type='line' if tier['point'] else 'rect',
                                 x0=max(a, start), x1=min(b, end), y0=0, y1=1,
                                 line={'color': '#747f88', 'width': 1},
                                 fillcolor='#fff4c2' if label else '#f1f3f5', layer='below', row=row, col=1)
        centers = [(max(a, start) + min(b, end)) / 2 for a, b, _ in visible]
        figure.add_trace(go.Scatter(
            x=centers, y=[0.5] * len(visible),
            mode='markers+text' if len(visible) <= 65 else 'markers',
            text=[html.escape(label) for _, _, label in visible], textposition='middle center',
            marker={'size': 12, 'color': '#b08a19', 'opacity': 0.35},
            customdata=[[a, b, html.escape(label)] for a, b, label in visible],
            hovertemplate='%{customdata[2]}<br>%{customdata[0]:.3f}–%{customdata[1]:.3f} s<extra></extra>',
            name=html.escape(tier['name']),
        ), row=row, col=1)
        figure.update_yaxes(title_text=html.escape(tier['name']), range=[0, 1], showticklabels=False,
                            showgrid=False, zeroline=False, fixedrange=True, row=row, col=1)
    figure.update_xaxes(range=[start, end], showgrid=True)
    figure.update_xaxes(title_text='Zeit (Sekunden)', row=len(tiers) + 1, col=1)
    figure.update_layout(height=300 + 115 * len(tiers), margin={'l': 70, 'r': 25, 't': 20, 'b': 45},
                          showlegend=False, template='plotly_white', dragmode='zoom', clickmode='event+select')
    return figure


def sources() -> tuple[bytes, bytes, str] | None:
    mode = st.sidebar.radio('Dateiquelle', ['Projektordner', 'Dateien hochladen'])
    if mode == 'Dateien hochladen':
        audio = st.sidebar.file_uploader('Audiodatei', type=sorted(s.lstrip('.') for s in AUDIO_SUFFIXES))
        grid = st.sidebar.file_uploader('TextGrid (auch ohne Dateiendung)')
        if audio is None or grid is None:
            st.info('Bitte eine Audiodatei und die zugehörige TextGrid-Datei hochladen.')
            return None
        return audio.getvalue(), grid.getvalue(), audio.name
    audio_dir = Path(st.sidebar.text_input('Audioordner', str(ROOT / 'audios'))).expanduser()
    grid_dir = Path(st.sidebar.text_input('TextGrid-Ordner', str(ROOT / 'textgrids'))).expanduser()
    if not audio_dir.is_dir() or not grid_dir.is_dir():
        st.warning('Bitte zwei vorhandene Ordner angeben.')
        return None
    audios = sorted(p for p in audio_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)
    audio_stems = {path.stem for path in audios}
    grids = sorted(p for p in grid_dir.iterdir() if p.is_file() and
                   (p.suffix.lower() == '.textgrid' or not p.suffix or p.name in audio_stems))
    if not audios or not grids:
        st.info('In den gewählten Ordnern werden mindestens eine Audiodatei und eine TextGrid-Datei benötigt.')
        return None
    grid_path = st.sidebar.selectbox('TextGrid', grids, format_func=lambda p: p.name)
    stem = grid_path.stem if grid_path.suffix.lower() == '.textgrid' else grid_path.name
    match = next((i for i, path in enumerate(audios) if path.stem == stem), 0)
    audio_path = st.sidebar.selectbox('Zugehöriges Audio', audios, index=match,
                                      format_func=lambda p: p.name, key=f'audio-{grid_path}')
    if audio_path.stem != stem:
        st.sidebar.warning('Die Dateinamen stimmen nicht überein. Bitte die Zuordnung prüfen.')
    return audio_path.read_bytes(), grid_path.read_bytes(), audio_path.name


def main() -> None:
    st.set_page_config(page_title='TextGrid · Audio', page_icon='🎧', layout='wide')
    st.title('TextGrid · Audio')
    st.caption('Wellenform und Annotationen auf einer gemeinsamen Zeitachse')
    try:
        source = sources()
        if source is None:
            return
        audio_data, grid_data, name = source
        with st.spinner('Audio und TextGrid laden …'):
            grid = read_grid(grid_data)
            samples = decode_audio(audio_data)
    except Exception as error:
        logger.exception('Audio oder TextGrid konnte nicht geladen werden.')
        if isinstance(error, FileNotFoundError) and error.filename == 'ffmpeg':
            st.error('FFmpeg fehlt. Bitte FFmpeg installieren und die App erneut starten.')
        elif isinstance(error, subprocess.CalledProcessError):
            st.error('FFmpeg konnte die Audiodatei nicht lesen.')
            st.code(error.stderr.decode('utf-8', errors='replace'))
        else:
            st.error(f'Dateien konnten nicht geladen werden: {error}')
        return
    duration = len(samples) / SAMPLE_RATE
    digest = hashlib.sha256(audio_data)
    digest.update(grid_data)
    identity = digest.hexdigest()[:16]
    st.subheader(name)
    st.caption(f'{duration:.2f} Sekunden Audio · {len(grid["tiers"])} Annotationsebenen')
    if abs(grid['end'] - duration) > 0.25 or abs(grid['start']) > 0.01:
        st.warning(f'TextGrid-Zeitbereich: {grid["start"]:.3f}–{grid["end"]:.3f} s; Audio: 0–{duration:.3f} s. Bitte die zeitliche Zuordnung prüfen.')
    analysis_tiers, identity = word_analysis(grid, samples, name, identity)
    if not analysis_tiers:
        return
    names = [tier['name'] for tier in analysis_tiers]
    chosen = st.sidebar.multiselect('Sichtbare Ebenen', names, default=names, key=f'tiers-{identity}')
    tiers = [tier for tier in analysis_tiers if tier['name'] in chosen]
    if not tiers:
        st.info('Bitte mindestens eine Annotationsebene auswählen.')
        return

    search = st.sidebar.text_input('Annotationen durchsuchen', key=f'search-{identity}')
    show_empty = st.sidebar.checkbox('Leere Intervalle zur Auswahl anbieten')
    entries = [(tier['name'], a, b, label) for tier in tiers for a, b, label in tier['entries']
               if (label or show_empty) and search.casefold() in label.casefold()]
    selected = st.sidebar.selectbox('Intervall / Zeitpunkt', range(len(entries)), index=None,
        placeholder='Annotation auswählen …', key=f'entry-{identity}-{chosen}-{search}-{show_empty}',
        format_func=lambda i: f'{entries[i][1]:.3f} s · {entries[i][0]} · {entries[i][3] or "(Pause)"}')
    start_key, end_key = f'start-{identity}', f'end-{identity}'
    st.session_state.setdefault(start_key, 0.0)
    st.session_state.setdefault(end_key, min(10.0, duration))
    if selected is not None and st.sidebar.button('Auswahl in der Zeitachse anzeigen'):
        _, a, b, _ = entries[selected]
        st.session_state[start_key] = max(0.0, min(a - 0.5, duration - 0.01))
        st.session_state[end_key] = min(duration, max(b + 0.5, st.session_state[start_key] + 0.01))
    left, right, full = st.columns([1, 1, 2])
    if full.button('Gesamte Aufnahme anzeigen'):
        st.session_state[start_key], st.session_state[end_key] = 0.0, duration
    start = left.number_input('Ansicht von (s)', min_value=0.0, max_value=duration, step=0.1, format='%.3f', key=start_key)
    end = right.number_input('Ansicht bis (s)', min_value=0.0, max_value=duration, step=0.1, format='%.3f', key=end_key)
    if end <= start:
        st.warning('Das Ende des Ausschnitts muss nach seinem Anfang liegen.')
        return
    st.caption('Ziehen zum Zoomen, Doppelklick zum Zurücksetzen. Auf einen Annotationspunkt klicken, um dessen Audio auszuwählen. Für beschriftete Intervalle einen kurzen Zeitbereich wählen.')
    event = st.plotly_chart(make_figure(samples, tiers, start, end), width='stretch',
                           on_select='rerun', selection_mode='points',
                           key=f'chart-{identity}-{start}-{end}-{chosen}-{selected}-{search}-{show_empty}')
    playback_start, playback_end = start, end
    playback_label = 'Sichtbarer Ausschnitt'
    if selected is not None:
        _, playback_start, playback_end, playback_label = entries[selected]
    for point in event.selection.points:
        if point.get('customdata'):
            playback_start, playback_end, playback_label = point['customdata']
            playback_label = html.unescape(playback_label)
    if playback_end == playback_start:
        playback_start, playback_end = playback_start - 0.25, playback_end + 0.25
    playback_start, playback_end = max(0.0, playback_start), min(duration, playback_end)
    st.markdown('**Ausgewählten Bereich abspielen**')
    st.caption(f'{playback_label or "Pause"} · {playback_start:.3f}–{playback_end:.3f} s')
    if playback_end > playback_start:
        st.audio(audio_clip(samples, playback_start, playback_end), format='audio/wav')
    else:
        st.warning('Diese Annotation liegt außerhalb der Audiodatei.')
    with st.expander('Sichtbaren Ausschnitt unabhängig von der Annotation abspielen'):
        st.audio(audio_clip(samples, start, end), format='audio/wav')
    with st.expander('Annotationen im sichtbaren Zeitbereich'):
        st.dataframe([{'Ebene': tier['name'], 'Start (s)': a, 'Ende (s)': b, 'Text': label}
                      for tier in tiers for a, b, label in tier['entries'] if b >= start and a <= end],
                     width='stretch', hide_index=True)
    st.caption('Audiowiedergabe und Wellenform verwenden Mono mit 22.050 Hz. Die Player-Zeit beginnt beim ausgewählten Ausschnitt bei 0. Die Anzeige dient zum Betrachten; Annotationen werden nicht bearbeitet.')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    main()
