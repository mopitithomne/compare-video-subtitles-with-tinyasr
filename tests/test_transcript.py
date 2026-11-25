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
