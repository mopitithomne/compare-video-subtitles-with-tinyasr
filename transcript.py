import html
import json
import logging
import os
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import pysrt
from jiwer import cer, process_words, wer


_VTT_TIMESTAMP_RE = re.compile(
    r'^(?P<start>(?:\d{2}:)?\d{2}:\d{2}\.\d+)\s+-->\s+'
    r'(?P<end>(?:\d{2}:)?\d{2}:\d{2}\.\d+)'
)
_VTT_INLINE_TIMESTAMP_RE = re.compile(r'<\d{2}:\d{2}:\d{2}\.\d+>')
_VTT_SHORT_CUE_SECONDS = 0.05
_NEMO_NORMALIZATION_LANGUAGES = frozenset({'ar', 'de', 'en', 'es', 'fr', 'pt', 'ru'})
logger = logging.getLogger(__name__)


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


def _normalization_language(language):
    if not isinstance(language, str):
        return None
    language = language.replace('_', '-').split('-', 1)[0].casefold()
    return language if language in _NEMO_NORMALIZATION_LANGUAGES else None


@lru_cache(maxsize=2)
def _text_normalizer(language):
    from nemo_text_processing.text_normalization.normalize import Normalizer

    cache_dir = Path(
        os.environ.get('NEMO_TEXT_CACHE_DIR', 'data/nemo_text_processing')
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info('Loading NeMo text normalization for %s.', language)
    return Normalizer(
        input_case='lower_cased',
        lang=language,
        cache_dir=str(cache_dir),
    )


@lru_cache(maxsize=2)
def _inverse_text_normalizer(language):
    from nemo_text_processing.inverse_text_normalization.inverse_normalize import (
        InverseNormalizer,
    )

    cache_dir = Path(
        os.environ.get('NEMO_TEXT_CACHE_DIR', 'data/nemo_text_processing')
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info('Loading NeMo inverse text normalization for %s.', language)
    return InverseNormalizer(
        input_case='lower_cased',
        lang=language,
        cache_dir=str(cache_dir),
    )


def _prepare_for_normalization(text):
    text = text.casefold()
    prepared = []
    for index, char in enumerate(text):
        if not unicodedata.category(char).startswith('P'):
            prepared.append(char)
            continue
        previous = text[index - 1] if index else ''
        following = text[index + 1] if index + 1 < len(text) else ''
        if char in '.,٫' and previous.isdigit() and following.isdigit():
            prepared.append(char)
        elif char in '.!?':
            prepared.append(char)
        elif char in "'’-" and previous.isalpha() and following.isalpha():
            prepared.append(char)
        else:
            prepared.append(' ')
    return re.sub(r'\s+', ' ', ''.join(prepared)).strip()


def _normalization_chunks(text, normalizer):
    sentences = normalizer.split_text_into_sentences(text)
    chunks = []
    current_sentences = []
    current_word_count = 0
    max_words = 400
    for sentence in sentences:
        words = sentence.split()
        if len(words) > max_words:
            if current_sentences:
                chunks.append(' '.join(current_sentences))
                current_sentences = []
                current_word_count = 0
            chunks.extend(
                ' '.join(words[index : index + max_words])
                for index in range(0, len(words), max_words)
            )
        elif current_word_count + len(words) > max_words:
            chunks.append(' '.join(current_sentences))
            current_sentences = [sentence]
            current_word_count = len(words)
        else:
            current_sentences.append(sentence)
            current_word_count += len(words)
    if current_sentences:
        chunks.append(' '.join(current_sentences))
    return chunks


def _record_alias(aliases, alias, identifier):
    key = tuple(normalize(alias).split())
    if key:
        aliases[key].add(identifier)


@lru_cache(maxsize=7)
def _cldr_comparison_data(language):
    from babel import Locale
    from babel.numbers import format_decimal, get_currency_name
    from babel.units import UnknownUnitError, format_unit

    locale = Locale.parse(language)
    currency_candidates = defaultdict(set)
    currency_names = {}
    currency_family_candidates = defaultdict(set)

    for code, display_name in locale.currencies.items():
        if len(code) != 3 or not code.isalpha():
            continue
        identifier = f'currency_{code.casefold()}'
        names = {display_name}
        for count in (1, 2):
            name = get_currency_name(code, count=count, locale=locale)
            if name:
                names.add(name)
        currency_names[code] = names
        for name in names:
            _record_alias(currency_candidates, name, identifier)
            for token in normalize(name).split():
                if len(token) > 3 and token.isalpha():
                    currency_family_candidates[token].add(code)
        _record_alias(currency_candidates, code, identifier)

    currency_aliases = {
        alias: next(iter(identifiers))
        for alias, identifiers in currency_candidates.items()
        if len(identifiers) == 1
    }
    family_tokens = {
        token: codes
        for token, codes in currency_family_candidates.items()
        if len(codes) >= 3
    }
    family_parents = {token: token for token in family_tokens}

    def find_family(token):
        while family_parents[token] != token:
            family_parents[token] = family_parents[family_parents[token]]
            token = family_parents[token]
        return token

    family_names = list(family_tokens)
    for index, first in enumerate(family_names):
        for second in family_names[index + 1:]:
            first_codes = family_tokens[first]
            second_codes = family_tokens[second]
            overlap = len(first_codes & second_codes) / len(
                first_codes | second_codes
            )
            # Inflected currency names may differ slightly in their CLDR code sets.
            if overlap >= 0.75:
                family_parents[find_family(second)] = find_family(first)

    currency_families = defaultdict(list)
    for token in family_tokens:
        currency_families[find_family(token)].append(token)
    for tokens in currency_families.values():
        identifier = f'currency_family_{min(tokens, key=len)}'
        for token in tokens:
            currency_aliases[(token,)] = identifier

    usd_name_tokens = {
        token
        for name in currency_names.get('USD', ())
        for token in normalize(name).split()
    }
    dollar_family_aliases = [
        alias[0]
        for alias, identifier in currency_aliases.items()
        if len(alias) == 1
        and identifier.startswith('currency_family_')
        and alias[0] in usd_name_tokens
    ]

    symbol_candidates = defaultdict(set)
    for code, symbol in locale.currency_symbols.items():
        if len(code) == 3 and code.isalpha() and symbol:
            symbol_candidates[symbol].add(code)
    symbol_replacements = {}
    for symbol, codes in symbol_candidates.items():
        if symbol == '$' and dollar_family_aliases:
            symbol_replacements[symbol] = max(
                dollar_family_aliases,
                key=len,
            )
        elif len(codes) == 1:
            code = next(iter(codes))
            name = get_currency_name(code, count=2, locale=locale)
            if name:
                symbol_replacements[symbol] = name
    if dollar_family_aliases:
        symbol_replacements['$'] = max(dollar_family_aliases, key=len)

    unit_candidates = defaultdict(set)
    for unit_id in locale.unit_display_names:
        for length in ('long', 'short'):
            for value in (0, 1, 2, 5):
                try:
                    formatted = format_unit(
                        value,
                        unit_id,
                        length=length,
                        locale=locale,
                    )
                except UnknownUnitError:
                    continue
                number = format_decimal(value, locale=locale)
                if formatted.startswith(number):
                    unit_name = formatted[len(number):]
                elif formatted.endswith(number):
                    unit_name = formatted[:-len(number)]
                else:
                    continue
                _record_alias(unit_candidates, unit_name, unit_id)

    unit_aliases = {
        alias: next(iter(identifiers))
        for alias, identifiers in unit_candidates.items()
        if len(identifiers) == 1
    }
    gram_alias = ('g',)
    # Case folding can collapse distinct narrow symbols, such as g and G.
    if 'mass-gram' in unit_candidates.get(gram_alias, set()):
        unit_aliases[gram_alias] = 'mass-gram'

    return locale, currency_aliases, unit_aliases, symbol_replacements


def _expand_currency_symbols(text, symbol_replacements):
    amount = r'\d+(?:[.,٫]\d+)*'
    for symbol, currency_name in sorted(
        symbol_replacements.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        escaped_symbol = re.escape(symbol)
        text = re.sub(
            rf'{escaped_symbol}\s*({amount})',
            rf'\1 {currency_name}',
            text,
        )
        text = re.sub(
            rf'({amount})\s*{escaped_symbol}',
            rf'\1 {currency_name}',
            text,
        )
        text = text.replace(symbol, f' {currency_name} ')
    return text


def _comparison_tokens(text):
    protected_punctuation = {
        ',': '\ue000',
        '.': '\ue001',
        '٫': '\ue002',
    }
    for punctuation, marker in protected_punctuation.items():
        text = re.sub(
            rf'(?<=\d){re.escape(punctuation)}(?=\d)',
            marker,
            text,
        )
    tokens = normalize(text).split()
    return [
        token.translate(str.maketrans({
            marker: punctuation
            for punctuation, marker in protected_punctuation.items()
        }))
        for token in tokens
    ]


def _matching_alias(tokens, start, aliases, max_alias_length):
    max_end = min(len(tokens), start + max_alias_length)
    for end in range(max_end, start, -1):
        identifier = aliases.get(tuple(tokens[start:end]))
        if identifier is not None:
            return identifier, end - start
    return None


def _quantity_token(kind, identifier, amount=None):
    identifier = re.sub(r'[^a-z0-9]', '', identifier.casefold())
    token = f'q{kind}{identifier}'
    if amount is not None:
        amount = amount.replace('-', 'minus').replace('.', 'point')
        token += f'v{amount}'
    return token


def _unparsed_comparison_token(token):
    return [part for part in re.split(r'[.,٫]', token) if part]


def _normalize_unmatched_numbers(tokens, language):
    number_pattern = re.compile(r'\d+(?:[.,٫]\d+)*')
    if not any(number_pattern.fullmatch(token) for token in tokens):
        return tokens

    normalizer = _text_normalizer(language)
    result = []
    for token in tokens:
        if number_pattern.fullmatch(token):
            normalized = normalizer.normalize(token, verbose=False)
            result.extend(_comparison_tokens(normalized))
        else:
            result.append(token)
    return result


def _quantity_amount(token, locale):
    from babel.numbers import NumberFormatError, parse_decimal

    if not re.fullmatch(r'\d+(?:[.,٫]\d+)*', token):
        return None
    try:
        return format(parse_decimal(token, locale=locale).normalize(), 'f')
    except NumberFormatError:
        return None


def _canonicalize_comparison_tokens(
    tokens,
    locale,
    currency_aliases,
    unit_aliases,
):
    result = []
    index = 0
    max_currency_alias_length = max(
        (len(alias) for alias in currency_aliases),
        default=0,
    )
    max_unit_alias_length = max(
        (len(alias) for alias in unit_aliases),
        default=0,
    )
    while index < len(tokens):
        token = tokens[index]
        amount = _quantity_amount(token, locale)

        if amount is not None:
            currency = _matching_alias(
                tokens,
                index + 1,
                currency_aliases,
                max_currency_alias_length,
            )
            unit = _matching_alias(
                tokens,
                index + 1,
                unit_aliases,
                max_unit_alias_length,
            )
            if currency and unit:
                result.append(token)
                index += 1
            elif currency:
                identifier, alias_length = currency
                result.append(_quantity_token('currency', identifier, amount))
                index += alias_length + 1
            elif unit:
                identifier, alias_length = unit
                result.append(_quantity_token('unit', identifier, amount))
                index += alias_length + 1
            else:
                result.append(token)
                index += 1
            continue

        currency = _matching_alias(
            tokens,
            index,
            currency_aliases,
            max_currency_alias_length,
        )
        unit = _matching_alias(
            tokens,
            index,
            unit_aliases,
            max_unit_alias_length,
        )
        if currency and unit:
            result.extend(tokens[index:index + max(currency[1], unit[1])])
            index += max(currency[1], unit[1])
        elif currency:
            identifier, alias_length = currency
            amount = (
                _quantity_amount(tokens[index + alias_length], locale)
                if index + alias_length < len(tokens)
                else None
            )
            if amount is not None:
                result.append(
                    _quantity_token('currency', identifier, amount),
                )
                index += alias_length + 1
            else:
                result.append(_quantity_token('currency', identifier))
                index += alias_length
        else:
            result.extend(_unparsed_comparison_token(token))
            index += 1
    return _normalize_unmatched_numbers(result, locale.language)


def _comparison_words(text, language=None):
    language = _normalization_language(language)
    if language is not None:
        locale, currency_aliases, unit_aliases, symbol_replacements = (
            _cldr_comparison_data(language)
        )
        normalizer = _inverse_text_normalizer(language)
        normalized = ' '.join(
            normalizer.inverse_normalize(
                _prepare_for_normalization(chunk),
                verbose=False,
            )
            for chunk in _normalization_chunks(text, normalizer)
        )
        normalized = _expand_currency_symbols(
            normalized,
            symbol_replacements,
        )
        tokens = _comparison_tokens(normalized)
        return _canonicalize_comparison_tokens(
            tokens,
            locale,
            currency_aliases,
            unit_aliases,
        )
    else:
        return normalize(text).split()


def _mapped_comparison_words(text, language=None):
    raw_spans = _word_spans(text)
    language = _normalization_language(language)
    if language is None:
        return [
            (word, index, index + 1)
            for index, (start, end) in enumerate(raw_spans)
            for word in _comparison_words(text[start:end])
        ]

    normalized_words = _comparison_words(text, language)
    if not raw_spans or not normalized_words:
        return []

    raw_words = [
        (word, raw_index)
        for raw_index, (start, end) in enumerate(raw_spans)
        for word in normalize(text[start:end]).split()
    ]
    raw_tokens = [word for word, _ in raw_words]
    mapped_words = []
    raw_cursor = 0
    normalized_cursor = 0

    def map_unmatched(raw_start, raw_end, normalized_start, normalized_end):
        if normalized_start == normalized_end:
            return
        if raw_start < raw_end:
            mapped_start = raw_words[raw_start][1]
            mapped_end = raw_words[raw_end - 1][1] + 1
        else:
            mapped_start = raw_words[max(0, raw_start - 1)][1]
            mapped_end = mapped_start + 1
        mapped_words.extend(
            (normalized_words[index], mapped_start, mapped_end)
            for index in range(normalized_start, normalized_end)
        )

    window_size = 256
    while normalized_cursor < len(normalized_words):
        if raw_cursor == len(raw_words):
            map_unmatched(
                raw_cursor,
                raw_cursor,
                normalized_cursor,
                len(normalized_words),
            )
            break
        raw_end = min(raw_cursor + window_size, len(raw_words))
        normalized_end = min(
            normalized_cursor + window_size,
            len(normalized_words),
        )
        matcher = SequenceMatcher(
            None,
            raw_tokens[raw_cursor:raw_end],
            normalized_words[normalized_cursor:normalized_end],
            autojunk=False,
        )
        blocks = [
            block
            for block in matcher.get_matching_blocks()
            if block.size
        ]
        if not blocks:
            map_unmatched(
                raw_cursor,
                raw_end,
                normalized_cursor,
                normalized_end,
            )
            raw_cursor = raw_end
            normalized_cursor = normalized_end
            continue

        window_raw_start = raw_cursor
        window_normalized_start = normalized_cursor
        for block in blocks:
            raw_match = window_raw_start + block.a
            normalized_match = window_normalized_start + block.b
            map_unmatched(
                raw_cursor,
                raw_match,
                normalized_cursor,
                normalized_match,
            )
            mapped_words.extend(
                (
                    normalized_words[normalized_match + offset],
                    raw_words[raw_match + offset][1],
                    raw_words[raw_match + offset][1] + 1,
                )
                for offset in range(block.size)
            )
            raw_cursor = raw_match + block.size
            normalized_cursor = normalized_match + block.size
    return mapped_words


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


def compare_texts(reference, hypothesis, language=None):
    reference = ' '.join(_comparison_words(reference, language))
    hypothesis = ' '.join(_comparison_words(hypothesis, language))
    if not reference or not hypothesis:
        raise ValueError('Both reference and TinyASR transcript must contain text.')
    return {
        'wer': wer(reference, hypothesis) * 100,
        'cer': cer(reference, hypothesis) * 100,
    }


def _raw_word_differences(
    reference_words,
    hypothesis_words,
    reference_raw_count,
    hypothesis_raw_count,
):
    if not reference_words or not hypothesis_words:
        raise ValueError('Both reference and TinyASR transcript must contain text.')
    alignment = process_words(
        ' '.join(word for word, _, _ in reference_words),
        ' '.join(word for word, _, _ in hypothesis_words),
    ).alignments[0]
    reference_states = [set() for _ in range(reference_raw_count)]
    hypothesis_states = [set() for _ in range(hypothesis_raw_count)]
    for chunk in alignment:
        different = chunk.type != 'equal'
        if chunk.type in {'equal', 'substitute'}:
            for index in range(chunk.ref_start_idx, chunk.ref_end_idx):
                _, raw_start, raw_end = reference_words[index]
                for raw_index in range(raw_start, raw_end):
                    reference_states[raw_index].add(different)
            for index in range(chunk.hyp_start_idx, chunk.hyp_end_idx):
                _, raw_start, raw_end = hypothesis_words[index]
                for raw_index in range(raw_start, raw_end):
                    hypothesis_states[raw_index].add(different)
        elif chunk.type == 'delete':
            for index in range(chunk.ref_start_idx, chunk.ref_end_idx):
                _, raw_start, raw_end = reference_words[index]
                for raw_index in range(raw_start, raw_end):
                    reference_states[raw_index].add(True)
        elif chunk.type == 'insert':
            for index in range(chunk.hyp_start_idx, chunk.hyp_end_idx):
                _, raw_start, raw_end = hypothesis_words[index]
                for raw_index in range(raw_start, raw_end):
                    hypothesis_states[raw_index].add(True)
    return (
        alignment,
        [not states or any(states) for states in reference_states],
        [not states or any(states) for states in hypothesis_states],
    )


def compare_subtitle_cues(cues, hypothesis, language=None):
    reference_words = [
        (word, cue_index, timestamp)
        for cue_index, cue in enumerate(cues)
        for word, timestamp in cue['words']
    ]
    hypothesis_words = _split_words(hypothesis)
    if not reference_words or not hypothesis_words:
        raise ValueError('Both reference and TinyASR transcript must contain text.')

    mapped_reference_words = _mapped_comparison_words(
        ' '.join(word for word, _, _ in reference_words),
        language,
    )
    mapped_hypothesis_words = _mapped_comparison_words(
        ' '.join(hypothesis_words),
        language,
    )
    if not mapped_reference_words or not mapped_hypothesis_words:
        raise ValueError('Normalization removed all comparison text.')
    alignment, _, hypothesis_differences = _raw_word_differences(
        mapped_reference_words,
        mapped_hypothesis_words,
        len(reference_words),
        len(hypothesis_words),
    )
    rows = [
        {
            'timestamp': None,
            'source': cue['text'],
            'tinyasr_parts': [],
            'has_differences': False,
        }
        for cue in cues
    ]
    hypothesis_cues = [None] * len(hypothesis_words)
    missing_reference_words = set()

    def append_word(cue_index, timestamp, word, different=False):
        row = rows[cue_index]
        if different and not row['has_differences']:
            row['timestamp'] = timestamp
            row['has_differences'] = True
        row['tinyasr_parts'].append({'text': word, 'different': different})

    for chunk in alignment:
        if chunk.type in {'equal', 'substitute'}:
            hyp_indices = range(chunk.hyp_start_idx, chunk.hyp_end_idx)
            for offset, hyp_index in enumerate(hyp_indices):
                ref_index = min(
                    chunk.ref_start_idx + offset,
                    chunk.ref_end_idx - 1,
                )
                _, cue_index, timestamp = reference_words[
                    mapped_reference_words[ref_index][1]
                ]
                _, raw_start, raw_end = mapped_hypothesis_words[hyp_index]
                for raw_index in range(raw_start, raw_end):
                    if hypothesis_cues[raw_index] is None:
                        hypothesis_cues[raw_index] = (cue_index, timestamp)
        elif chunk.type == 'delete':
            for ref_index in range(chunk.ref_start_idx, chunk.ref_end_idx):
                _, raw_start, raw_end = mapped_reference_words[ref_index]
                missing_reference_words.update(range(raw_start, raw_end))
        elif chunk.type == 'insert':
            ref_index = max(0, chunk.ref_start_idx - 1)
            _, cue_index, timestamp = reference_words[
                mapped_reference_words[ref_index][1]
            ]
            for hyp_index in range(chunk.hyp_start_idx, chunk.hyp_end_idx):
                _, raw_start, raw_end = mapped_hypothesis_words[hyp_index]
                for raw_index in range(raw_start, raw_end):
                    if hypothesis_cues[raw_index] is None:
                        hypothesis_cues[raw_index] = (cue_index, timestamp)

    last_cue = (0, cues[0]['timestamp'])
    for raw_index, word in enumerate(hypothesis_words):
        location = hypothesis_cues[raw_index]
        if location is None:
            location = last_cue
            hypothesis_differences[raw_index] = True
        last_cue = location
        cue_index, timestamp = location
        append_word(
            cue_index,
            timestamp,
            word,
            hypothesis_differences[raw_index],
        )

    for raw_index in sorted(missing_reference_words):
        source_word, cue_index, timestamp = reference_words[raw_index]
        append_word(
            cue_index,
            timestamp,
            f'[missing: {source_word}]',
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


def _render_aligned_side(text, spans, differences, character_changes):
    rows = []
    parts = []
    cursor = 0
    row_bytes = 0
    row_words = 0

    def finish_row():
        nonlocal parts, row_bytes, row_words
        if row_words:
            rows.append(parts)
        parts = []
        row_bytes = 0
        row_words = 0

    for index, (start, end) in enumerate(spans):
        prefix = text[cursor:start]
        word = text[start:end]
        part_bytes = len(prefix.encode('utf-8')) + len(word.encode('utf-8'))
        if row_words and (
            row_words >= 100 or row_bytes + part_bytes > 6_000
        ):
            finish_row()

        _append_alignment_part(parts, prefix, False)
        _append_alignment_part(
            parts,
            word,
            differences[index],
            character_changes[index],
        )
        cursor = end
        row_bytes += part_bytes
        row_words += 1

    tail = text[cursor:]
    if tail:
        _append_alignment_part(parts, tail, False)
    finish_row()
    return rows


def _align_normalized_texts(source_text, hypothesis, language):
    source_spans = _word_spans(source_text)
    hypothesis_spans = _word_spans(hypothesis)
    source_words = [source_text[start:end] for start, end in source_spans]
    hypothesis_words = [hypothesis[start:end] for start, end in hypothesis_spans]
    if not source_words or not hypothesis_words:
        raise ValueError('Both reference and TinyASR transcript must contain text.')

    mapped_source = _mapped_comparison_words(source_text, language)
    mapped_hypothesis = _mapped_comparison_words(hypothesis, language)
    if not mapped_source or not mapped_hypothesis:
        raise ValueError('Normalization removed all comparison text.')
    _, source_differences, hypothesis_differences = _raw_word_differences(
        mapped_source,
        mapped_hypothesis,
        len(source_words),
        len(hypothesis_words),
    )

    source_character_changes = [None] * len(source_words)
    hypothesis_character_changes = [None] * len(hypothesis_words)
    raw_alignment = process_words(
        normalize(' '.join(source_words)),
        normalize(' '.join(hypothesis_words)),
    ).alignments[0]
    for chunk in raw_alignment:
        if chunk.type == 'equal':
            continue
        pair_count = max(
            chunk.ref_end_idx - chunk.ref_start_idx,
            chunk.hyp_end_idx - chunk.hyp_start_idx,
        )
        for offset in range(pair_count):
            source_index = chunk.ref_start_idx + offset
            hypothesis_index = chunk.hyp_start_idx + offset
            if (
                source_index >= chunk.ref_end_idx
                or hypothesis_index >= chunk.hyp_end_idx
                or not source_differences[source_index]
                or not hypothesis_differences[hypothesis_index]
            ):
                continue
            character_changes = _single_character_change_ranges(
                source_words[source_index],
                hypothesis_words[hypothesis_index],
            )
            if character_changes is not None:
                source_character_changes[source_index] = character_changes[0]
                hypothesis_character_changes[hypothesis_index] = character_changes[1]

    source_rows = _render_aligned_side(
        source_text,
        source_spans,
        source_differences,
        source_character_changes,
    )
    hypothesis_rows = _render_aligned_side(
        hypothesis,
        hypothesis_spans,
        hypothesis_differences,
        hypothesis_character_changes,
    )
    return [
        {
            'source_parts': (
                source_rows[index] if index < len(source_rows) else []
            ),
            'tinyasr_parts': (
                hypothesis_rows[index] if index < len(hypothesis_rows) else []
            ),
        }
        for index in range(max(len(source_rows), len(hypothesis_rows)))
    ]


def align_transcript_texts(source_text, hypothesis, language=None):
    if _normalization_language(language) is not None:
        return _align_normalized_texts(source_text, hypothesis, language)

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


def write_difference_files(output_dir, title, cues, hypothesis, language=None):
    output_dir = Path(output_dir)
    differences = compare_subtitle_cues(cues, hypothesis, language)
    json_path = output_dir / 'subtitle-differences.json'
    json_path.write_text(
        json.dumps(differences, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )

    source_text = ' '.join(cue['text'] for cue in cues)
    aligned_rows = align_transcript_texts(source_text, hypothesis, language)
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
