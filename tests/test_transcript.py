import tempfile
import unittest
from pathlib import Path

from transcript import (
    align_transcript_texts,
    compare_subtitle_cues,
    compare_texts,
    format_timestamp_mmss,
    read_reference,
    read_subtitle_cues,
    write_difference_files,
)


class TranscriptTests(unittest.TestCase):
    def test_formats_display_timestamp_as_total_minutes_and_seconds(self):
        self.assertEqual(format_timestamp_mmss('01:02:03.456'), '62:02')
        self.assertEqual(format_timestamp_mmss('00:00:59.999'), '00:58')
        self.assertEqual(format_timestamp_mmss('00:00:00.500'), '00:00')

    def test_vtt_strips_timing_and_duplicate_rollup_cues(self):
        content = '\n'.join(
            (
                'WEBVTT',
                '',
                '00:00:00.000 --> 00:00:01.000',
                ' ',
                'I<00:00:00.200><c> feel</c><00:00:00.400><c> fine.</c>',
                '',
                '00:00:01.000 --> 00:00:01.010',
                'I feel fine.',
                '',
                '00:00:01.010 --> 00:00:02.000',
                'I feel fine.',
                'I<00:00:01.200><c> am leaving</c><00:00:01.400><c> now.</c>',
                '',
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'subtitles.vtt'
            path.write_text(content, encoding='utf-8')

            self.assertEqual(
                read_reference(path),
                'I feel fine. I am leaving now.',
            )

    def test_srt_timestamps_are_not_part_of_the_reference(self):
        content = (
            '1\n'
            '00:00:01,000 --> 00:00:02,000\n'
            'Hello world.\n'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'subtitles.srt'
            path.write_text(content, encoding='utf-8')

            self.assertEqual(read_reference(path), 'Hello world.')

    def test_comparison_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            compare_texts('', 'hello')

    def test_language_aware_normalization_matches_currency_and_units(self):
        self.assertEqual(
            compare_texts('$', 'dollar', 'en'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('€', 'euro', 'fr'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('$5', '5 dollars', 'en'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('€5', '5 euros', 'fr'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('5 g', '5 grams', 'en'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('5 g', '5 grammes', 'fr'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('cinq euros', '5 €', 'fr'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('cent grammes', '100 g', 'fr'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('five dollars', '$5', 'en'),
            {'wer': 0, 'cer': 0},
        )
        long_reference = ' '.join(['hello'] * 401 + ['five dollars'])
        long_hypothesis = ' '.join(['hello'] * 401 + ['$5'])
        self.assertEqual(
            compare_texts(long_reference, long_hypothesis, 'en'),
            {'wer': 0, 'cer': 0},
        )
        self.assertGreater(compare_texts('100 g', '100 kg', 'en')['wer'], 0)
        self.assertGreater(compare_texts('five dollars', '$5', 'it')['wer'], 0)

    def test_cldr_aliases_preserve_currency_and_amount_distinctions(self):
        self.assertEqual(
            compare_texts('5 apples', 'five apples', 'en'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('5 km', '5 Kilometer', 'de'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('5 £', '5 GBP', 'de'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('$5', '5 dollars', 'fr'),
            {'wer': 0, 'cer': 0},
        )
        self.assertEqual(
            compare_texts('$1.50', '1.5 dollars', 'en'),
            {'wer': 0, 'cer': 0},
        )
        self.assertGreater(compare_texts('5 USD', '5 CAD', 'en')['wer'], 0)
        self.assertGreater(compare_texts('5 dollars', '6 dollars', 'en')['wer'], 0)
        self.assertGreater(compare_texts('$5', '5 USD', 'en')['wer'], 0)

    def test_normalized_report_keeps_raw_text_without_highlighting_equivalences(self):
        cues = [
            {
                'timestamp': '00:00:01.000',
                'text': 'cinq euros',
                'words': [
                    ('cinq', '00:00:01.000'),
                    ('euros', '00:00:01.000'),
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            differences, html_path, _ = write_difference_files(
                temp_dir,
                'Currency',
                cues,
                '5 €',
                'fr',
            )
            report = html_path.read_text(encoding='utf-8')
            aligned_rows = align_transcript_texts('cinq euros', '5 €', 'fr')

        self.assertEqual(differences, [])
        self.assertIn('<td><pre class="transcript">cinq euros</pre>', report)
        self.assertIn('<td><pre class="transcript">5 €</pre>', report)
        self.assertNotIn('<span class="source-difference">', report)
        self.assertNotIn('<span class="tinyasr-difference">', report)
        self.assertEqual(
            ''.join(
                part['text']
                for row in aligned_rows
                for part in row['source_parts']
            ),
            'cinq euros',
        )
        self.assertEqual(
            ''.join(
                part['text']
                for row in aligned_rows
                for part in row['tinyasr_parts']
            ),
            '5 €',
        )
        self.assertFalse(any(
            part['different']
            for row in aligned_rows
            for side in ('source_parts', 'tinyasr_parts')
            for part in row[side]
        ))

    def test_normalization_alignment_handles_multiword_number_conversion(self):
        source_text = 'one hundred grams'
        tinyasr_text = '100 g'
        cues = [
            {
                'timestamp': '00:00:01.000',
                'text': source_text,
                'words': [
                    ('one', '00:00:01.000'),
                    ('hundred', '00:00:01.000'),
                    ('grams', '00:00:01.000'),
                ],
            }
        ]
        rows = align_transcript_texts(source_text, tinyasr_text, 'en')

        self.assertEqual(compare_subtitle_cues(cues, tinyasr_text, 'en'), [])
        self.assertEqual(
            ''.join(
                part['text']
                for row in rows
                for part in row['source_parts']
            ),
            source_text,
        )
        self.assertEqual(
            ''.join(
                part['text']
                for row in rows
                for part in row['tinyasr_parts']
            ),
            tinyasr_text,
        )
        self.assertFalse(any(
            part['different']
            for row in rows
            for side in ('source_parts', 'tinyasr_parts')
            for part in row[side]
        ))

        currency_rows = align_transcript_texts('five dollars', '$5', 'en')
        self.assertEqual(
            ''.join(
                part['text']
                for row in currency_rows
                for part in row['source_parts']
            ),
            'five dollars',
        )
        self.assertEqual(
            ''.join(
                part['text']
                for row in currency_rows
                for part in row['tinyasr_parts']
            ),
            '$5',
        )
        self.assertFalse(any(
            part['different']
            for row in currency_rows
            for side in ('source_parts', 'tinyasr_parts')
            for part in row[side]
        ))

    def test_comparison_uses_inline_vtt_word_timestamps(self):
        content = (
            'WEBVTT\n\n'
            '00:00:01.000 --> 00:00:03.000\n'
            'The<00:00:01.100><c> cat</c><00:00:01.500><c> sleeps.</c>\n'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'subtitles.vtt'
            path.write_text(content, encoding='utf-8')
            cues = read_subtitle_cues(path)

        self.assertEqual(
            cues[0]['words'],
            [
                ('The', '00:00:01.000'),
                ('cat', '00:00:01.100'),
                ('sleeps', '00:00:01.500'),
            ],
        )
        differences = compare_subtitle_cues(cues, 'The cat sleep')
        self.assertEqual(differences[0]['timestamp'], '00:00:01.500')
        self.assertEqual(differences[0]['source'], 'The cat sleeps.')
        self.assertEqual(
            differences[0]['tinyasr_parts'][-1],
            {'text': 'sleep', 'different': True},
        )

    def test_comparison_reports_insertions_and_deletions(self):
        cues = [
            {
                'timestamp': '00:00:02.000',
                'text': 'We say go.',
                'words': [
                    ('We', '00:00:02.000'),
                    ('say', '00:00:02.500'),
                    ('go', '00:00:03.000'),
                ],
            }
        ]

        differences = compare_subtitle_cues(cues, 'We often say')

        self.assertEqual(differences[0]['timestamp'], '00:00:02.000')
        self.assertEqual(
            differences[0]['tinyasr_parts'],
            [
                {'text': 'We', 'different': False},
                {'text': 'often', 'different': True},
                {'text': 'say', 'different': False},
                {'text': '[missing: go]', 'different': True},
            ],
        )

    def test_full_alignment_preserves_text_and_marks_differences_on_both_sides(self):
        source_text = 'Hello, brave world!'
        tinyasr_text = 'Hello, kind world! Again.'

        rows = align_transcript_texts(source_text, tinyasr_text)
        source_parts = [
            part for row in rows for part in row['source_parts']
        ]
        tinyasr_parts = [
            part for row in rows for part in row['tinyasr_parts']
        ]

        self.assertEqual(''.join(part['text'] for part in source_parts), source_text)
        self.assertEqual(''.join(part['text'] for part in tinyasr_parts), tinyasr_text)
        self.assertIn(
            ('brave', True),
            [(part['text'], part['different']) for part in source_parts],
        )
        self.assertIn(
            ('kind', True),
            [(part['text'], part['different']) for part in tinyasr_parts],
        )
        self.assertIn(
            ('Again', True),
            [(part['text'], part['different']) for part in tinyasr_parts],
        )

    def test_html_highlights_single_character_changes_in_light_blue(self):
        cues = [
            {
                'timestamp': '00:00:01.000',
                'text': 'cat dog',
                'words': [
                    ('cat', '00:00:01.000'),
                    ('dog', '00:00:01.000'),
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            _, html_path, _ = write_difference_files(
                temp_dir,
                'Single character',
                cues,
                'cut dogs',
            )
            report = html_path.read_text(encoding='utf-8')
        rows = align_transcript_texts('cat dog', 'cut dogs')

        source_parts = [part for row in rows for part in row['source_parts']]
        tinyasr_parts = [part for row in rows for part in row['tinyasr_parts']]
        self.assertIn([[1, 2]], [
            part['character_changes'] for part in source_parts
            if part['text'] == 'cat'
        ])
        self.assertIn([[1, 2]], [
            part['character_changes'] for part in tinyasr_parts
            if part['text'] == 'cut'
        ])
        self.assertIn(
            '<span class="source-difference">c'
            '<span class="character-difference">a</span>t</span>',
            report,
        )
        self.assertIn(
            '<span class="tinyasr-difference">c'
            '<span class="character-difference">u</span>t</span>',
            report,
        )
        self.assertIn(
            '<span class="tinyasr-difference">dog'
            '<span class="character-difference">s</span></span>',
            report,
        )
        self.assertIn('.character-difference{background:#b3e5fc;', report)

        deletion_rows = align_transcript_texts('dogs', 'dog')
        deletion_source = [
            part for row in deletion_rows for part in row['source_parts']
        ]
        self.assertIn(
            [[3, 4]],
            [
                part['character_changes']
                for part in deletion_source
                if part['text'] == 'dogs'
            ],
        )

    def test_html_report_shows_full_text_side_by_side_with_colored_differences(self):
        cues = [
            {
                'timestamp': '00:00:01.000',
                'text': 'Hello, brave world!',
                'words': [
                    ('Hello', '00:00:01.000'),
                    ('brave', '00:00:01.000'),
                    ('world', '00:00:01.000'),
                ],
            },
            {
                'timestamp': '00:00:02.000',
                'text': 'Stay safe.',
                'words': [
                    ('Stay', '00:00:02.000'),
                    ('safe', '00:00:02.000'),
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            _, html_path, _ = write_difference_files(
                temp_dir,
                '<Video>',
                cues,
                'Hello, kind world! Stay safe and sound.',
            )
            report = html_path.read_text(encoding='utf-8')

        self.assertIn('<td><pre class="transcript">Hello, ', report)
        self.assertIn(
            '<span class="source-difference">brave</span> world! Stay safe.',
            report,
        )
        self.assertIn(
            '<span class="tinyasr-difference">kind</span> world! Stay safe ',
            report,
        )
        self.assertIn('<span class="tinyasr-difference">and</span>', report)
        self.assertIn('<span class="tinyasr-difference">sound</span>', report)
        self.assertNotIn('<span class="character-difference">', report)
        self.assertIn('2 of 2 subtitle cues differ.', report)
        self.assertIn('&lt;Video&gt;', report)


if __name__ == '__main__':
    unittest.main()
