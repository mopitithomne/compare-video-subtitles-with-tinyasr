import html
import json
import re
import unicodedata
from pathlib import Path

import pysrt
from jiwer import cer, process_words, wer


_VTT_TIMESTAMP_RE = re.compile(
    r'^(?P<start>(?:\d{2}:)?\d{2}:\d{2}\.\d+)\s+-->\s+'
    r'(?P<end>(?:\d{2}:)?\d{2}:\d{2}\.\d+)'
)
_VTT_INLINE_TIMESTAMP_RE = re.compile(r'<\d{2}:\d{2}:\d{2}\.\d+>')
_VTT_SHORT_CUE_SECONDS = 0.05


def _split_words(text):
    words = []
    current = []
    for char in text:
        if char.isspace() or unicodedata.category(char).startswith('P'):
            if current:
                words.append(''.join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append(''.join(current))
    return words


def _word_spans(text):
    spans = []
    start = None
    for index, char in enumerate(text):
        if char.isspace() or unicodedata.category(char).startswith('P'):
            if start is not None:
                spans.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        spans.append((start, len(text)))
    return spans


def normalize(text):
    text = text.casefold()
    text = ''.join(
        ' ' if unicodedata.category(char).startswith('P') else char
        for char in text
    )
    return re.sub(r'\s+', ' ', text).strip()


def _timestamp_seconds(timestamp):
    parts = [float(part) for part in timestamp.split(':')]
    return sum(
        part * 60 ** (len(parts) - index - 1)
        for index, part in enumerate(parts)
    )


def format_timestamp_mmss(timestamp):
    total_seconds = max(0, int(_timestamp_seconds(timestamp)) - 1)
    minutes, seconds = divmod(total_seconds, 60)
    return f'{minutes:02}:{seconds:02}'


def _vtt_timestamped_words(line, fallback_timestamp):
    words = []
    timestamp = fallback_timestamp
    for part in re.split(f'({_VTT_INLINE_TIMESTAMP_RE.pattern})', line):
        if _VTT_INLINE_TIMESTAMP_RE.fullmatch(part):
            timestamp = part[1:-1]
            continue
        text = html.unescape(re.sub(r'<[^>]*>', '', part))
        words.extend((word, timestamp) for word in _split_words(text))
    return words


def _read_vtt_cues(path):
    content = path.read_text(encoding='utf-8-sig')
    cues = []
    current_cue = None
    for line in content.splitlines():
        match = _VTT_TIMESTAMP_RE.match(line.strip())
        if match:
            if current_cue is not None:
                cues.append(current_cue)
            current_cue = (match['start'], match['end'], [])
        elif line == '':
            if current_cue is not None:
                cues.append(current_cue)
                current_cue = None
        elif current_cue is not None:
            current_cue[2].append(line)
    if current_cue is not None:
        cues.append(current_cue)

    prepared_cues = []
    for start, end, raw_lines in cues:
        cleaned_lines = [
            (line, html.unescape(re.sub(r'<[^>]*>', '', line)).strip())
            for line in raw_lines
        ]
        cleaned_lines = [(raw, text) for raw, text in cleaned_lines if text]
        timed_lines = [
            text
            for raw, text in cleaned_lines
            if _VTT_INLINE_TIMESTAMP_RE.search(raw)
        ]
        cue_lines = timed_lines or [text for _, text in cleaned_lines]
        duration = _timestamp_seconds(end) - _timestamp_seconds(start)
        prepared_cues.append(
            (
                start,
                duration,
                bool(timed_lines),
                cue_lines,
                [text for _, text in cleaned_lines],
                raw_lines,
            )
        )

    captions = []
    for index, (
        start,
        duration,
        has_word_timing,
        cue_lines,
        cleaned_lines,
        raw_lines,
    ) in enumerate(
        prepared_cues
    ):
        cue_text = normalize(' '.join(cue_lines))
        if not cue_text:
            continue
        if not has_word_timing and duration <= _VTT_SHORT_CUE_SECONDS:
            neighbors = prepared_cues[max(0, index - 1) : index] + prepared_cues[
                index + 1 : index + 2
            ]
            is_duplicate = any(
                cue_text == normalize(' '.join(neighbor_lines))
                or cue_text
                in {normalize(line) for line in neighbor_cleaned_lines}
                for _, _, _, neighbor_lines, neighbor_cleaned_lines, _ in neighbors
            )
            if is_duplicate:
                continue
        if has_word_timing:
            words = [
                word
                for raw_line in raw_lines
                if _VTT_INLINE_TIMESTAMP_RE.search(raw_line)
                for word in _vtt_timestamped_words(raw_line, start)
            ]
        else:
            words = [
                (word, start)
                for word in _split_words(' '.join(cleaned_lines))
            ]
        captions.append(
            {
                'timestamp': start,
                'text': ' '.join(cue_lines),
                'words': words,
            }
        )
    return captions


def _read_vtt(path):
    return ' '.join(cue['text'] for cue in _read_vtt_cues(path))


def _format_timestamp(milliseconds):
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f'{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}'


def read_subtitle_cues(path):
    path = Path(path)
    if path.suffix.casefold() == '.vtt':
        return _read_vtt_cues(path)
    if path.suffix.casefold() == '.srt':
        return [
            {
                'timestamp': _format_timestamp(subtitle.start.ordinal),
                'text': subtitle.text.replace('\n', ' '),
                'words': [
                    (word, _format_timestamp(subtitle.start.ordinal))
                    for word in _split_words(subtitle.text)
                ],
            }
            for subtitle in pysrt.open(str(path), encoding='utf-8')
        ]
    text = path.read_text(encoding='utf-8')
    return [
        {
            'timestamp': '00:00:00.000',
            'text': text,
            'words': [
                (word, '00:00:00.000')
                for word in _split_words(text)
            ],
        }
    ]


def read_reference(path):
    path = Path(path)
    if path.suffix.casefold() == '.srt':
        subtitles = pysrt.open(str(path), encoding='utf-8')
        return ' '.join(subtitle.text.replace('\n', ' ') for subtitle in subtitles)
    if path.suffix.casefold() == '.vtt':
        return _read_vtt(path)
    return path.read_text(encoding='utf-8')


def compare_texts(reference, hypothesis):
    reference = normalize(reference)
    hypothesis = normalize(hypothesis)
    if not reference or not hypothesis:
        raise ValueError('Both reference and TinyASR transcript must contain text.')
    return {
        'wer': wer(reference, hypothesis) * 100,
        'cer': cer(reference, hypothesis) * 100,
    }


def compare_subtitle_cues(cues, hypothesis):
    reference_words = [
        (word, cue_index, timestamp)
        for cue_index, cue in enumerate(cues)
        for word, timestamp in cue['words']
    ]
    hypothesis_words = _split_words(hypothesis)
    if not reference_words or not hypothesis_words:
        raise ValueError('Both reference and TinyASR transcript must contain text.')

    reference = normalize(' '.join(word for word, _, _ in reference_words))
    normalized_hypothesis = normalize(' '.join(hypothesis_words))
    alignment = process_words(reference, normalized_hypothesis).alignments[0]
    rows = [
        {
            'timestamp': None,
            'source': cue['text'],
            'tinyasr_parts': [],
            'has_differences': False,
        }
        for cue in cues
    ]

    def append_word(cue_index, timestamp, word, different=False):
        row = rows[cue_index]
        if different and not row['has_differences']:
            row['timestamp'] = timestamp
            row['has_differences'] = True
        row['tinyasr_parts'].append({'text': word, 'different': different})

    for chunk in alignment:
        if chunk.type in {'equal', 'substitute'}:
            different = chunk.type == 'substitute'
            for ref_index, hyp_index in zip(
                range(chunk.ref_start_idx, chunk.ref_end_idx),
                range(chunk.hyp_start_idx, chunk.hyp_end_idx),
            ):
                _, cue_index, timestamp = reference_words[ref_index]
                append_word(
                    cue_index,
                    timestamp,
                    hypothesis_words[hyp_index],
                    different,
                )
        elif chunk.type == 'delete':
            for ref_index in range(chunk.ref_start_idx, chunk.ref_end_idx):
                source_word, cue_index, timestamp = reference_words[ref_index]
                append_word(
                    cue_index,
                    timestamp,
                    f'[missing: {source_word}]',
                    True,
                )
        elif chunk.type == 'insert':
            ref_index = max(0, chunk.ref_start_idx - 1)
            _, cue_index, timestamp = reference_words[ref_index]
            for hyp_index in range(chunk.hyp_start_idx, chunk.hyp_end_idx):
                append_word(
                    cue_index,
                    timestamp,
                    hypothesis_words[hyp_index],
                    True,
                )

    return [
        {
            'timestamp': row['timestamp'],
            'source': row['source'],
            'tinyasr': ' '.join(part['text'] for part in row['tinyasr_parts']),
            'tinyasr_parts': row['tinyasr_parts'],
        }
        for row in rows
        if row['has_differences']
    ]


def _append_alignment_part(parts, text, different, character_change=None):
    if not text:
        return
    if parts and parts[-1]['different'] == different:
        part = parts[-1]
        offset = len(part['text'])
        part['text'] += text
    else:
        part = {'text': text, 'different': different}
        parts.append(part)
        offset = 0
    if character_change and character_change[0] < character_change[1]:
        part.setdefault('character_changes', []).append([
            offset + character_change[0],
            offset + character_change[1],
        ])


def _single_character_change_ranges(source_word, hypothesis_word):
    if abs(len(source_word) - len(hypothesis_word)) > 1:
        return None

    source_index = 0
    hypothesis_index = 0
    source_change = None
    hypothesis_change = None
    while source_index < len(source_word) and hypothesis_index < len(hypothesis_word):
        if source_word[source_index] == hypothesis_word[hypothesis_index]:
            source_index += 1
            hypothesis_index += 1
            continue
        if source_change is not None:
            return None
        if len(source_word) > len(hypothesis_word):
            source_change = (source_index, source_index + 1)
            hypothesis_change = (hypothesis_index, hypothesis_index)
            source_index += 1
        elif len(hypothesis_word) > len(source_word):
            source_change = (source_index, source_index)
            hypothesis_change = (hypothesis_index, hypothesis_index + 1)
            hypothesis_index += 1
        else:
            source_change = (source_index, source_index + 1)
            hypothesis_change = (hypothesis_index, hypothesis_index + 1)
            source_index += 1
            hypothesis_index += 1

    if source_index < len(source_word):
        if source_change is not None or source_index + 1 != len(source_word):
            return None
        source_change = (source_index, source_index + 1)
        hypothesis_change = (hypothesis_index, hypothesis_index)
    elif hypothesis_index < len(hypothesis_word):
        if source_change is not None or hypothesis_index + 1 != len(hypothesis_word):
            return None
        source_change = (source_index, source_index)
        hypothesis_change = (hypothesis_index, hypothesis_index + 1)

    if source_change is None:
        return None
    return source_change, hypothesis_change


def align_transcript_texts(source_text, hypothesis):
    source_spans = _word_spans(source_text)
    hypothesis_spans = _word_spans(hypothesis)
    source_words = [source_text[start:end] for start, end in source_spans]
    hypothesis_words = [hypothesis[start:end] for start, end in hypothesis_spans]
    if not source_words or not hypothesis_words:
        raise ValueError('Both reference and TinyASR transcript must contain text.')
    alignment = process_words(
        normalize(' '.join(source_words)),
        normalize(' '.join(hypothesis_words)),
    ).alignments[0]

    rows = []
    current_row = {'source_parts': [], 'tinyasr_parts': []}
    current_row_bytes = 0
    current_row_words = 0
    source_cursor = 0
    hypothesis_cursor = 0

    def finish_row():
        nonlocal current_row, current_row_bytes, current_row_words
        if current_row_words:
            rows.append(current_row)
        current_row = {'source_parts': [], 'tinyasr_parts': []}
        current_row_bytes = 0
        current_row_words = 0

    for chunk in alignment:
        source_indices = range(chunk.ref_start_idx, chunk.ref_end_idx)
        hypothesis_indices = range(chunk.hyp_start_idx, chunk.hyp_end_idx)
        pair_count = max(len(source_indices), len(hypothesis_indices))
        different = chunk.type != 'equal'
        for offset in range(pair_count):
            source_index = (
                source_indices[offset] if offset < len(source_indices) else None
            )
            hypothesis_index = (
                hypothesis_indices[offset]
                if offset < len(hypothesis_indices)
                else None
            )
            source_prefix = ''
            source_word = ''
            if source_index is not None:
                start, end = source_spans[source_index]
                source_prefix = source_text[source_cursor:start]
                source_word = source_text[start:end]
                source_cursor = end
            hypothesis_prefix = ''
            hypothesis_word = ''
            if hypothesis_index is not None:
                start, end = hypothesis_spans[hypothesis_index]
                hypothesis_prefix = hypothesis[hypothesis_cursor:start]
                hypothesis_word = hypothesis[start:end]
                hypothesis_cursor = end
            character_changes = (
                _single_character_change_ranges(source_word, hypothesis_word)
                if different and source_word and hypothesis_word
                else None
            )

            pair_bytes = sum(
                len(part.encode('utf-8'))
                for part in (
                    source_prefix,
                    source_word,
                    hypothesis_prefix,
                    hypothesis_word,
                )
            )
            if current_row_words and (
                current_row_words >= 100 or current_row_bytes + pair_bytes > 6_000
            ):
                finish_row()

            _append_alignment_part(
                current_row['source_parts'],
                source_prefix,
                False,
            )
            _append_alignment_part(
                current_row['source_parts'],
                source_word,
                different and source_index is not None,
                character_changes[0] if character_changes else None,
            )
            _append_alignment_part(
                current_row['tinyasr_parts'],
                hypothesis_prefix,
                False,
            )
            _append_alignment_part(
                current_row['tinyasr_parts'],
                hypothesis_word,
                different and hypothesis_index is not None,
                character_changes[1] if character_changes else None,
            )
            current_row_bytes += pair_bytes
            current_row_words += 1

    source_tail = source_text[source_cursor:]
    hypothesis_tail = hypothesis[hypothesis_cursor:]
    if source_tail or hypothesis_tail:
        _append_alignment_part(current_row['source_parts'], source_tail, False)
        _append_alignment_part(
            current_row['tinyasr_parts'],
            hypothesis_tail,
            False,
        )
    finish_row()
    return rows


def _render_alignment_parts(parts, css_class):
    rendered = []
    for part in parts:
        text = part['text']
        changes = part.get('character_changes', [])
        if changes:
            text_parts = []
            cursor = 0
            for start, end in changes:
                text_parts.append(html.escape(text[cursor:start]))
                text_parts.append(
                    f'<span class="character-difference">'
                    f'{html.escape(text[start:end])}</span>'
                )
                cursor = end
            text_parts.append(html.escape(text[cursor:]))
            text = ''.join(text_parts)
        if part['different']:
            rendered.append(f'<span class="{css_class}">{text}</span>')
        else:
            rendered.append(text)
    return ''.join(rendered)


def write_difference_files(output_dir, title, cues, hypothesis):
    output_dir = Path(output_dir)
    differences = compare_subtitle_cues(cues, hypothesis)
    json_path = output_dir / 'subtitle-differences.json'
    json_path.write_text(
        json.dumps(differences, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )

    source_text = ' '.join(cue['text'] for cue in cues)
    aligned_rows = align_transcript_texts(source_text, hypothesis)
    alignment_path = output_dir / 'subtitle-alignment.json'
    alignment_path.write_text(
        json.dumps(aligned_rows, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    source_html = ''.join(
        _render_alignment_parts(row['source_parts'], 'source-difference')
        for row in aligned_rows
    )
    hypothesis_html = ''.join(
        _render_alignment_parts(row['tinyasr_parts'], 'tinyasr-difference')
        for row in aligned_rows
    )
    html_path = output_dir / 'subtitle-differences.html'
    html_path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        f'<title>Subtitle differences: {html.escape(title)}</title>'
        '<style>body{font:16px sans-serif;margin:2em}table{border-collapse:collapse;'
        'width:100%}th,td{border:1px solid #bbb;padding:.5em;text-align:left;'
        'vertical-align:top;width:50%}th{background:#eee}.transcript{white-space:'
        'pre-wrap;overflow-wrap:anywhere;margin:0;font:inherit}.source-difference{'
        'color:#a61b1b;background:#fee4e2}.tinyasr-difference{color:#137333;'
        'background:#dcfae6}.character-difference{background:#b3e5fc;'
        'color:inherit}@media(max-width:700px){table,tbody,tr,td{display:block}'
        'th{width:50%}}</style>'
        f'<h1>Subtitle differences: {html.escape(title)}</h1>'
        f'<p>{len(differences)} of {len(cues)} subtitle cues differ. '
        'Cue counts are not a word-level match percentage.</p>'
        '<p>Red marks source words that differ or are missing in TinyASR; green '
        'marks differing or additional TinyASR words. Light blue highlights a '
        'single changed character within a differing word.</p>'
        '<table><thead><tr><th>YouTube subtitles</th><th>TinyASR transcript</th>'
        f'</tr></thead><tbody><tr><td><pre class="transcript">{source_html}</pre>'
        f'</td><td><pre class="transcript">{hypothesis_html}</pre></td></tr>'
        '</tbody></table></html>\n',
        encoding='utf-8',
    )
    return differences, html_path, json_path
