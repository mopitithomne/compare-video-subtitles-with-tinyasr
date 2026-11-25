import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import (
    AudioLanguageError,
    MAX_TELEGRAM_UPLOAD_BYTES,
    _download_inputs,
    _extract_info,
    _telegram_audio_file,
    inspect_video,
    process_video,
    select_audio_format,
)


class AudioFormatTests(unittest.TestCase):
    def test_inspect_video_accepts_a_duration_of_one_hour(self):
        with patch(
            'pipeline._extract_info',
            return_value={
                'duration': 3_600,
                'title': 'One-hour video',
                'formats': [
                    {
                        'format_id': 'en',
                        'acodec': 'opus',
                        'vcodec': 'none',
                        'language': 'en',
                    }
                ],
                'subtitles': {'en': [{}]},
            },
        ):
            details = inspect_video('https://www.youtube.com/watch?v=M8qOjBw2Vo8')

        self.assertEqual(details['duration'], 3_600)
        self.assertEqual([track['language_code'] for track in details['tracks']], ['en'])

    def test_inspect_video_only_offers_subtitles_matching_audio_language(self):
        info = {
            'duration': 60,
            'formats': [
                {
                    'format_id': 'audio-en',
                    'acodec': 'opus',
                    'vcodec': 'none',
                    'language': 'en-US',
                }
            ],
            'subtitles': {'en': [{}], 'fr': [{}]},
            'automatic_captions': {'de': [{}]},
        }
        with patch('pipeline._extract_info', return_value=info):
            details = inspect_video('https://www.youtube.com/watch?v=M8qOjBw2Vo8')

        self.assertEqual(
            [(track['language_code'], track['source']) for track in details['tracks']],
            [('en', 'manual')],
        )

    def test_inspect_video_rejects_unidentified_audio_language(self):
        with (
            patch(
                'pipeline._extract_info',
                return_value={
                    'duration': 60,
                    'subtitles': {'en': [{}]},
                },
            ),
            self.assertRaisesRegex(AudioLanguageError, 'Could not identify'),
        ):
            inspect_video('https://www.youtube.com/watch?v=M8qOjBw2Vo8')

    def test_inspect_video_rejects_a_duration_over_one_hour(self):
        with (
            patch(
                'pipeline._extract_info',
                return_value={'duration': 3_601, 'title': 'Over-one-hour video'},
            ),
            self.assertRaisesRegex(ValueError, '60-minute limit'),
        ):
            inspect_video('https://www.youtube.com/watch?v=M8qOjBw2Vo8')

    def test_passes_configured_cookie_file_to_yt_dlp(self):
        cookie_file = '/app/secrets/youtube-cookies.txt'
        with (
            patch.dict(os.environ, {'YTDLP_COOKIES_FILE': cookie_file}),
            patch('pipeline.YoutubeDL') as youtube_dl,
        ):
            youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
                'id': 'M8qOjBw2Vo8'
            }
            _extract_info('https://www.youtube.com/watch?v=M8qOjBw2Vo8')

        options = youtube_dl.call_args.args[0]
        self.assertEqual(options['cookiefile'], cookie_file)

    def test_does_not_pass_cookie_file_when_unconfigured(self):
        with (
            patch.dict(os.environ, {'YTDLP_COOKIES_FILE': ''}),
            patch('pipeline.YoutubeDL') as youtube_dl,
        ):
            youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {}
            _extract_info('https://www.youtube.com/watch?v=M8qOjBw2Vo8')

        options = youtube_dl.call_args.args[0]
        self.assertNotIn('cookiefile', options)

    def test_download_inputs_passes_configured_cookie_file_to_yt_dlp(self):
        cookie_file = '/app/secrets/youtube-cookies.txt'
        track = {
            'language_code': 'en',
            'subtitle_code': 'en',
            'source': 'manual',
        }
        with (
            patch.dict(os.environ, {'YTDLP_COOKIES_FILE': cookie_file}),
            patch(
                'pipeline._extract_info',
                return_value={
                    'id': 'M8qOjBw2Vo8',
                    'formats': [],
                    'language': 'en',
                },
            ),
            patch('pipeline.YoutubeDL') as youtube_dl,
        ):
            youtube_dl.return_value.__enter__.return_value.extract_info.side_effect = (
                RuntimeError('stop after checking options')
            )
            with self.assertRaisesRegex(RuntimeError, 'stop after checking options'):
                _download_inputs(
                    'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
                    track,
                    Path('/tmp'),
                )

        options = youtube_dl.call_args.args[0]
        self.assertEqual(options['cookiefile'], cookie_file)

    def test_compresses_large_audio_for_telegram_delivery(self):
        with tempfile.TemporaryDirectory() as output_dir:
            audio_path = Path(output_dir) / 'audio.webm'
            with audio_path.open('wb') as audio_file:
                audio_file.truncate(MAX_TELEGRAM_UPLOAD_BYTES + 1)

            def create_compressed_audio(command, **kwargs):
                Path(command[-1]).write_bytes(b'mp3 audio')

            with patch('pipeline.subprocess.run', side_effect=create_compressed_audio) as run:
                delivery_path, compressed = _telegram_audio_file(
                    audio_path,
                    Path(output_dir),
                )

        self.assertTrue(compressed)
        self.assertEqual(delivery_path.name, 'audio-for-telegram.mp3')
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('-b:a') + 1], '64k')

    def test_converts_small_non_mp3_audio_for_telegram_playback(self):
        with tempfile.TemporaryDirectory() as output_dir:
            audio_path = Path(output_dir) / 'audio.webm'
            audio_path.write_bytes(b'audio bytes')

            def create_mp3(command, **kwargs):
                Path(command[-1]).write_bytes(b'mp3 audio')

            with patch('pipeline.subprocess.run', side_effect=create_mp3) as run:
                delivery_path, converted = _telegram_audio_file(
                    audio_path,
                    Path(output_dir),
                )

        self.assertTrue(converted)
        self.assertEqual(delivery_path.suffix, '.mp3')
        self.assertEqual(
            run.call_args.args[0][run.call_args.args[0].index('-f') + 1],
            'mp3',
        )

    def test_reuses_small_mp3_audio_for_telegram_playback(self):
        with tempfile.TemporaryDirectory() as output_dir:
            audio_path = Path(output_dir) / 'audio.mp3'
            audio_path.write_bytes(b'mp3 audio')

            with patch('pipeline.subprocess.run') as run:
                delivery_path, converted = _telegram_audio_file(
                    audio_path,
                    Path(output_dir),
                )

        self.assertFalse(converted)
        self.assertEqual(delivery_path, audio_path)
        run.assert_not_called()

    def test_process_video_logs_transcription_and_comparison_stages(self):
        progress_updates = []
        with tempfile.TemporaryDirectory() as output_dir:
            audio_path = Path(output_dir) / 'audio.mp3'
            audio_path.write_bytes(b'audio')
            subtitle_path = Path(output_dir) / 'subtitles.vtt'
            with (
                patch(
                    'pipeline._download_inputs',
                    return_value=(audio_path, subtitle_path, 'Video title'),
                ),
                patch('pipeline.read_reference', return_value='hello world'),
                patch(
                    'pipeline.read_subtitle_cues',
                    return_value=[
                        {
                            'timestamp': '00:00:01.000',
                            'text': 'hello world',
                            'words': [
                                ('hello', '00:00:01.000'),
                                ('world', '00:00:02.000'),
                            ],
                        }
                    ],
                ),
                patch('pipeline.transcribe', return_value='hello there'),
                self.assertLogs('pipeline', level='INFO') as captured,
            ):
                result = process_video(
                    'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
                    {
                        'model_label': 'English, Russian, Ukrainian',
                        'model_language_code': 'en',
                    },
                    output_dir,
                    progress_updates.append,
                )

            self.assertEqual(result['wer'], 50.0)
            self.assertEqual(result['audio_path'], audio_path)
            self.assertEqual(result['subtitle_path'], subtitle_path)
            self.assertEqual(
                Path(result['transcript_path']).read_text(encoding='utf-8'),
                'hello there\n',
            )
            self.assertIn(
                '<span class="tinyasr-difference">there</span>',
                Path(result['differences_html_path']).read_text(encoding='utf-8'),
            )
            self.assertIn(
                '<span class="source-difference">world</span>',
                Path(result['differences_html_path']).read_text(encoding='utf-8'),
            )
            aligned_rows = json.loads(
                Path(result['alignment_path']).read_text(encoding='utf-8')
            )
            self.assertEqual(
                ''.join(
                    part['text']
                    for row in aligned_rows
                    for part in row['source_parts']
                ),
                'hello world',
            )
            self.assertEqual(
                ''.join(
                    part['text']
                    for row in aligned_rows
                    for part in row['tinyasr_parts']
                ),
                'hello there',
            )
            differences = json.loads(
                Path(result['differences_json_path']).read_text(encoding='utf-8')
            )
            self.assertEqual(result['differences'], differences)
            self.assertEqual(result['difference_count'], 1)
            self.assertEqual(result['subtitle_cue_count'], 1)
            self.assertEqual(differences[0]['timestamp'], '00:00:02.000')
            self.assertEqual(differences[0]['source'], 'hello world')

        self.assertIn('Starting TinyASR transcription', '\n'.join(captured.output))
        self.assertIn('Comparing transcript with subtitle reference', '\n'.join(captured.output))
        self.assertIn('Text comparison complete', '\n'.join(captured.output))
        self.assertIn('Step 2/5: Preparing TinyASR and loading its model...', progress_updates)
        self.assertIn('Step 4/5: Comparing the transcription with subtitles...', progress_updates)
        self.assertIn('Step 5/5: Preparing result files for Telegram...', progress_updates)

    def test_selects_highest_bitrate_track_matching_subtitle_language(self):
        info = {
            'formats': [
                {
                    'format_id': 'low-fr',
                    'acodec': 'opus',
                    'vcodec': 'none',
                    'language': 'fr',
                    'abr': 64,
                },
                {
                    'format_id': 'high-fr',
                    'acodec': 'opus',
                    'vcodec': 'none',
                    'language': 'fr-CA',
                    'abr': 128,
                },
                {
                    'format_id': 'en',
                    'acodec': 'opus',
                    'vcodec': 'none',
                    'language': 'en',
                    'abr': 192,
                },
            ]
        }

        self.assertEqual(select_audio_format(info, 'fr'), 'high-fr')

    def test_refuses_a_mismatched_tagged_audio_track(self):
        info = {
            'formats': [
                {
                    'format_id': 'en',
                    'acodec': 'opus',
                    'vcodec': 'none',
                    'language': 'en',
                    'abr': 128,
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, 'No YouTube audio track'):
            select_audio_format(info, 'fr')

    def test_uses_default_audio_when_top_level_language_matches(self):
        info = {
            'language': 'fr',
            'formats': [
                {
                    'format_id': 'audio',
                    'acodec': 'opus',
                    'vcodec': 'none',
                }
            ]
        }

        self.assertEqual(select_audio_format(info, 'fr'), 'bestaudio/best')

    def test_refuses_audio_when_language_cannot_be_verified(self):
        info = {
            'formats': [
                {
                    'format_id': 'audio',
                    'acodec': 'opus',
                    'vcodec': 'none',
                }
            ]
        }

        with self.assertRaisesRegex(AudioLanguageError, 'Could not verify'):
            select_audio_format(info, 'fr')


if __name__ == '__main__':
    unittest.main()
