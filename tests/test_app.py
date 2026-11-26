from io import BytesIO
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import app as bot_app


class LocalBotTests(unittest.TestCase):
    def setUp(self):
        self.owner_environment = patch.dict(os.environ, {'BOT_OWNER': '42'})
        self.owner_environment.start()
        self.store = bot_app.JobStore(':memory:')
        bot_app._store = self.store

    def tearDown(self):
        self.store.close()
        bot_app._store = None
        self.owner_environment.stop()

    def _approve_user(self, user_id, chat_id=None):
        self.store.request_user_access(
            user_id,
            user_id if chat_id is None else chat_id,
            f'Test user {user_id}',
            None,
            None,
        )
        self.store.approve_user(user_id)

    def _save_sample_report(self, reports_directory, user_id=42):
        report_id = 'a' * 32
        files = [
            'audio.mp3',
            'source-subtitles.vtt',
            'tinyasr-transcript.txt',
            'subtitle-differences.html',
            'subtitle-alignment.json',
        ]
        report_directory = Path(reports_directory) / report_id
        report_directory.mkdir(parents=True)
        for filename in files:
            contents = (
                json.dumps([
                    {
                        'source_parts': [
                            {'text': 'hello ', 'different': False},
                            {'text': 'world', 'different': True},
                        ],
                        'tinyasr_parts': [
                            {'text': 'hello ', 'different': False},
                            {'text': 'there', 'different': True},
                        ],
                    }
                ])
                if filename == 'subtitle-alignment.json'
                else 'saved report'
            )
            (report_directory / filename).write_text(contents, encoding='utf-8')
        report = {
            'report_id': report_id,
            'video_id': 'dQw4w9WgXcQ',
            'youtube_url': 'https://youtu.be/dQw4w9WgXcQ',
            'title': 'Saved video',
            'track': {'language_name': 'English', 'source': 'manual'},
            'wer': 10.0,
            'cer': 5.0,
            'difference_count': 1,
            'subtitle_cue_count': 10,
            'summary': (
                'Comparison complete: Saved video\nWER: 10.00% | CER: 5.00%'
            ),
            'files': files,
            'created_at': bot_app._now(),
        }
        self.store.save_report(report, user_id)
        return report

    def test_id_command_does_not_expose_telegram_id(self):
        update = {
            'update_id': 720,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': '/id',
            },
        }
        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        send_message.assert_called_once_with(
            42,
            'Send a YouTube video link or upload a Telegram video, voice message, '
            'or audio file (up to 20 MB and 60 minutes). Use /help for instructions.',
        )
        submit_job.assert_not_called()

    def test_help_explains_wer_and_cer_without_id_command(self):
        with patch('app._send_message') as send_message:
            bot_app._send_help(42)

        help_text = send_message.call_args.args[1]
        self.assertIn('Word Error Rate', help_text)
        self.assertIn('Character Error Rate', help_text)
        self.assertIn('YouTube video link', help_text)
        self.assertIn('voice message, audio or video directly', help_text)
        self.assertIn('detects the spoken language automatically', help_text)
        self.assertIn('creator-provided, manually reviewed subtitles', help_text)
        self.assertIn('educational, lecture, news, or organization channels', help_text)
        self.assertIn('channel type alone does not guarantee accuracy', help_text)
        self.assertIn('"manual" subtitle track over "automatic"', help_text)
        self.assertIn('/reports', help_text)
        self.assertNotIn('/id', help_text)

    def test_start_does_not_explain_wer_or_cer(self):
        update = {
            'update_id': 829,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': '/start',
            },
        }
        with patch('app._send_message') as send_message:
            bot_app._handle_update(update)

        start_text = send_message.call_args.args[1]
        self.assertIn('Send either:', start_text)
        self.assertNotIn('Word Error Rate', start_text)
        self.assertNotIn('Character Error Rate', start_text)
        self.assertNotIn('WER (', start_text)
        self.assertNotIn('CER (', start_text)

    def test_language_keyboard_source_labels_are_english(self):
        keyboard = bot_app._language_keyboard(
            'a' * 32,
            [
                {'language_name': 'French', 'source': 'manual'},
                {'language_name': 'French', 'source': 'automatic'},
            ],
        )

        self.assertEqual(
            [button[0]['text'] for button in keyboard['inline_keyboard']],
            ['French (manual)', 'French (automatic)'],
        )

    def test_duplicate_message_update_creates_only_one_job(self):
        update = {
            'update_id': 721,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': 'Compare https://youtu.be/dQw4w9WgXcQ',
            },
        }
        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)
            bot_app._handle_update(update)

        jobs = self.store.active_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'discovering')
        submit_job.assert_called_once_with(bot_app._discover_job, jobs[0]['job_id'])
        self.assertEqual(send_message.call_count, 2)

    def test_new_user_requires_owner_approval_before_using_the_bot(self):
        first_update = {
            'update_id': 724,
            'message': {
                'from': {
                    'id': 321,
                    'first_name': 'Alex',
                    'last_name': 'Example',
                    'username': 'alex_example',
                },
                'chat': {'id': 321},
                'text': '/start',
            },
        }
        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(first_update)
            bot_app._handle_update(
                {
                    'update_id': 725,
                    'message': {
                        'from': first_update['message']['from'],
                        'chat': {'id': 321},
                        'text': 'https://youtu.be/dQw4w9WgXcQ',
                    },
                }
            )

        self.assertEqual(self.store.request_user_access(
            321,
            321,
            'Alex',
            'Example',
            'alex_example',
        )['status'], 'pending')
        self.assertEqual(send_message.call_count, 3)
        owner_notice = send_message.call_args_list[0]
        self.assertEqual(owner_notice.args[0], 42)
        self.assertIn('Name: Alex Example', owner_notice.args[1])
        self.assertIn('Username: @alex_example', owner_notice.args[1])
        self.assertIn('Telegram ID: 321', owner_notice.args[1])
        self.assertEqual(
            owner_notice.args[2]['inline_keyboard'][0][0]['callback_data'],
            'access|321',
        )
        self.assertEqual(send_message.call_args_list[1].args[0], 321)
        self.assertIn('requires owner approval', send_message.call_args_list[1].args[1])
        self.assertEqual(send_message.call_args_list[2].args[0], 321)
        submit_job.assert_not_called()

        callback = {
            'id': 'approve-user',
            'data': 'access|321',
            'from': {'id': 42},
            'message': {'chat': {'id': 42}},
        }
        with (
            patch('app._answer_callback') as answer_callback,
            patch('app._send_message') as approval_message,
        ):
            bot_app._handle_callback_query(callback)

        self.assertTrue(self.store.user_is_approved(321))
        answer_callback.assert_called_once_with('approve-user', 'Access approved.')
        approval_message.assert_called_once_with(
            321,
            'Your access has been approved. Send /start to start using the bot.',
        )

        with (
            patch('app._send_message') as accepted_message,
            patch('app._submit_job') as accepted_submit,
        ):
            bot_app._handle_update(
                {
                    'update_id': 726,
                    'message': {
                        'from': first_update['message']['from'],
                        'chat': {'id': 321},
                        'text': 'https://youtu.be/dQw4w9WgXcQ',
                    },
                }
            )

        self.assertEqual(self.store.active_jobs()[0]['status'], 'discovering')
        self.assertIn('added to the queue', accepted_message.call_args.args[1])
        self.assertNotIn('Queue:', accepted_message.call_args.args[1])
        accepted_submit.assert_called_once_with(
            bot_app._discover_job,
            self.store.active_jobs()[0]['job_id'],
        )

    def test_owner_can_list_approved_users_and_remove_access(self):
        self.store.request_user_access(321, 321, 'Alex', 'Example', 'alex_example')
        self.store.approve_user(321)
        list_update = {
            'update_id': 830,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': '/users',
            },
        }
        with patch('app._send_message') as send_message:
            bot_app._handle_update(list_update)

        send_message.assert_called_once()
        self.assertIn('Approved users', send_message.call_args.args[1])
        self.assertIn('Alex Example', send_message.call_args.args[1])
        self.assertIn('@alex_example', send_message.call_args.args[1])
        self.assertIn('ID: 321', send_message.call_args.args[1])
        remove_button = send_message.call_args.args[2]['inline_keyboard'][0][0]
        self.assertEqual(remove_button['text'], 'Remove Alex Example')
        self.assertEqual(remove_button['callback_data'], 'remove_user|321')

        callback = {
            'id': 'remove-approved-user',
            'data': 'remove_user|321',
            'from': {'id': 42},
            'message': {'chat': {'id': 42}},
        }
        with (
            patch('app._answer_callback') as answer_callback,
            patch('app._send_message') as send_message,
        ):
            bot_app._handle_callback_query(callback)

        self.assertFalse(self.store.user_is_approved(321))
        answer_callback.assert_called_once_with(
            'remove-approved-user',
            'User access removed.',
        )
        send_message.assert_any_call(
            321,
            'Your access to this bot has been revoked. Contact the owner if you need access again.',
        )
        self.assertEqual(
            send_message.call_args_list[-1].args[1],
            'There are no approved users.',
        )
        access_request = self.store.request_user_access(
            321,
            321,
            'Alex',
            'Example',
            'alex_example',
        )
        self.assertEqual(access_request, {'status': 'pending', 'notify_owner': True})

    def test_non_owner_cannot_list_or_remove_approved_users(self):
        self._approve_user(321)
        with patch('app._send_message') as send_message:
            bot_app._handle_update(
                {
                    'update_id': 831,
                    'message': {
                        'from': {'id': 321},
                        'chat': {'id': 321},
                        'text': '/users',
                    },
                }
            )

        send_message.assert_called_once_with(
            321,
            'Only the owner can manage approved users.',
        )

        callback = {
            'id': 'unauthorized-remove',
            'data': 'remove_user|321',
            'from': {'id': 777},
            'message': {'chat': {'id': 777}},
        }
        with (
            patch('app._answer_callback') as answer_callback,
            patch('app._send_message') as send_message,
        ):
            bot_app._handle_callback_query(callback)

        self.assertTrue(self.store.user_is_approved(321))
        answer_callback.assert_called_once_with(
            'unauthorized-remove',
            'Only the owner can remove access from the private chat.',
        )
        send_message.assert_not_called()

    def test_telegram_video_upload_is_queued_for_transcription(self):
        update = {
            'update_id': 727,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'video': {
                    'file_id': 'telegram-file-id',
                    'file_size': 1024,
                    'duration': 12,
                },
            },
        }
        progress_callback = lambda text: True
        with (
            patch(
                'app._send_progress_message',
                return_value=progress_callback,
            ) as send_progress,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        submit_job.assert_called_once()
        function, job_id, callback = submit_job.call_args.args
        self.assertIs(function, bot_app._process_telegram_video_job)
        self.assertIs(callback, progress_callback)
        job = self.store.get_job(job_id)
        self.assertEqual(job['source_type'], 'telegram')
        self.assertEqual(job['telegram_file_id'], 'telegram-file-id')
        self.assertEqual(job['status'], 'queued')
        self.assertIsNone(job['selected_track'])
        self.assertIn(
            'detect the spoken language automatically',
            send_progress.call_args.args[1],
        )
        self.assertNotIn('Queue:', send_progress.call_args.args[1])

    def test_telegram_voice_and_audio_uploads_are_queued_for_transcription(self):
        self._approve_user(43)
        document = {
            'file_id': 'telegram-audio-document-id',
            'mime_type': 'audio/mpeg',
        }
        self.assertEqual(
            bot_app._telegram_media_attachment({'document': document}),
            (document, 'audio'),
        )
        updates = [
            {
                'update_id': 827,
                'message': {
                    'from': {'id': 42},
                    'chat': {'id': 42},
                    'voice': {
                        'file_id': 'telegram-voice-id',
                        'file_size': 512,
                        'duration': 6,
                    },
                },
            },
            {
                'update_id': 828,
                'message': {
                    'from': {'id': 43},
                    'chat': {'id': 43},
                    'audio': {
                        'file_id': 'telegram-audio-id',
                        'file_size': 1024,
                        'duration': 12,
                    },
                },
            },
        ]
        progress_callback = lambda _text: True
        with (
            patch(
                'app._send_progress_message',
                return_value=progress_callback,
            ) as send_progress,
            patch('app._submit_job') as submit_job,
        ):
            for update in updates:
                bot_app._handle_update(update)

        self.assertEqual(submit_job.call_count, 2)
        for call, expected_file_id in zip(
            submit_job.call_args_list,
            ('telegram-voice-id', 'telegram-audio-id'),
        ):
            function, job_id, callback = call.args
            self.assertIs(function, bot_app._process_telegram_audio_job)
            self.assertIs(callback, progress_callback)
            job = self.store.get_job(job_id)
            self.assertEqual(job['source_type'], 'telegram_audio')
            self.assertEqual(job['telegram_file_id'], expected_file_id)
            self.assertEqual(job['status'], 'queued')
        self.assertEqual(send_progress.call_count, 2)
        self.assertIn(
            'Voice message received and queued',
            send_progress.call_args_list[0].args[1],
        )
        self.assertIn(
            'Audio file received and queued',
            send_progress.call_args_list[1].args[1],
        )

    def test_oversized_telegram_video_is_rejected_without_queueing(self):
        update = {
            'update_id': 728,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'video': {
                    'file_id': 'large-telegram-file',
                    'file_size': 20 * 1024 * 1024 + 1,
                },
            },
        }
        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        self.assertIn('larger than Telegram bot downloads support', send_message.call_args.args[1])
        self.assertEqual(self.store.active_jobs(), [])
        submit_job.assert_not_called()

    def test_downloads_telegram_file_to_a_temporary_video_path(self):
        class TelegramFileResponse(BytesIO):
            headers = {'Content-Length': '5'}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token'}),
            patch(
                'app._telegram_call',
                return_value={'file_path': 'videos/file.mp4', 'file_size': 5},
            ) as get_file,
            patch(
                'app.urllib.request.urlopen',
                return_value=TelegramFileResponse(b'video'),
            ) as open_file,
        ):
            video_path = bot_app._download_telegram_video(
                'telegram-file-id',
                temp_dir,
            )
            video_bytes = video_path.read_bytes()

        get_file.assert_called_once_with('getFile', {'file_id': 'telegram-file-id'})
        self.assertIn(
            '/file/bottest-token/videos/file.mp4',
            open_file.call_args.args[0].full_url,
        )
        self.assertEqual(video_bytes, b'video')

    def test_download_rejects_parent_paths_from_telegram(self):
        with (
            patch(
                'app._telegram_call',
                return_value={'file_path': '../outside.mp4', 'file_size': 5},
            ),
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(RuntimeError, 'invalid media download path'),
        ):
            bot_app._download_telegram_video('telegram-file-id', temp_dir)

    def test_transcript_draft_updates_reuse_one_nonzero_draft_id(self):
        with patch('app._telegram_call', return_value=True) as telegram_call:
            update_draft = bot_app._transcript_draft_callback(42)
            self.assertTrue(update_draft('First words'))
            self.assertTrue(update_draft('First words'))
            self.assertTrue(update_draft('x' * 5_000))

        self.assertEqual(telegram_call.call_count, 2)
        first_payload = telegram_call.call_args_list[0].args[1]
        second_payload = telegram_call.call_args_list[1].args[1]
        self.assertEqual(telegram_call.call_args_list[0].args[0], 'sendMessageDraft')
        self.assertEqual(first_payload['chat_id'], 42)
        self.assertGreater(first_payload['draft_id'], 0)
        self.assertEqual(first_payload['draft_id'], second_payload['draft_id'])
        self.assertEqual(len(second_payload['text']), 4_096)

    def test_uploaded_video_job_downloads_transcribes_and_sends_text(self):
        job_id, _ = self.store.create_job(
            729,
            'telegram-upload',
            42,
            42,
            source_type='telegram',
            telegram_file_id='telegram-file-id',
        )

        def write_transcript(_video_path, output_dir, _progress, transcript_callback):
            transcript_path = Path(output_dir) / 'tinyasr-transcript.txt'
            transcript_path.write_text('Hello from the video.', encoding='utf-8')
            transcript_callback('Hello from the video.')
            return transcript_path

        draft_callback = lambda _text: True
        with (
            self.assertLogs('telegram_asr_bot', level='INFO') as completion_logs,
            patch(
                'app._download_telegram_video',
                return_value=Path('video.mp4'),
            ) as download_video,
            patch(
                'app._transcript_draft_callback',
                return_value=draft_callback,
            ),
            patch(
                'app.transcribe_uploaded_video',
                side_effect=write_transcript,
            ) as transcribe_video,
            patch('app._send_message') as send_message,
        ):
            bot_app._process_telegram_video_job(job_id)

        self.assertEqual(self.store.get_job(job_id)['status'], 'completed')
        self.assertIn(
            'Telegram video transcription completed for job',
            '\n'.join(completion_logs.output),
        )
        self.assertIn(
            'No active video jobs remain; the bot can be safely restarted.',
            '\n'.join(completion_logs.output),
        )
        download_video.assert_called_once()
        transcribe_video.assert_called_once_with(
            Path('video.mp4'),
            ANY,
            None,
            draft_callback,
        )
        send_message.assert_called_once_with(
            42,
            'Transcript:\n\nHello from the video.',
        )

    def test_uploaded_audio_job_downloads_transcribes_and_sends_text(self):
        job_id, _ = self.store.create_job(
            830,
            'telegram-upload',
            42,
            42,
            source_type='telegram_audio',
            telegram_file_id='telegram-audio-id',
        )

        def write_transcript(_audio_path, output_dir, _progress, transcript_callback):
            transcript_path = Path(output_dir) / 'tinyasr-transcript.txt'
            transcript_path.write_text('Hello from the audio.', encoding='utf-8')
            transcript_callback('Hello from the audio.')
            return transcript_path

        draft_callback = lambda _text: True
        with (
            self.assertLogs('telegram_asr_bot', level='INFO') as completion_logs,
            patch(
                'app._download_telegram_audio',
                return_value=Path('voice.oga'),
            ) as download_audio,
            patch(
                'app._transcript_draft_callback',
                return_value=draft_callback,
            ),
            patch(
                'app.transcribe_uploaded_audio',
                side_effect=write_transcript,
            ) as transcribe_audio,
            patch('app._send_message') as send_message,
        ):
            bot_app._process_telegram_audio_job(job_id)

        self.assertEqual(self.store.get_job(job_id)['status'], 'completed')
        self.assertIn(
            'Telegram audio transcription completed for job',
            '\n'.join(completion_logs.output),
        )
        download_audio.assert_called_once()
        transcribe_audio.assert_called_once_with(
            Path('voice.oga'),
            ANY,
            None,
            draft_callback,
        )
        send_message.assert_called_once_with(
            42,
            'Transcript:\n\nHello from the audio.',
        )

    def test_transcript_output_is_split_to_fit_telegram_message_limits(self):
        transcript = 'a' * (bot_app._TRANSCRIPT_MESSAGE_CHARS + 10)
        with patch('app._send_message') as send_message:
            bot_app._send_transcript(42, transcript)

        self.assertEqual(send_message.call_count, 2)
        messages = [call.args[1] for call in send_message.call_args_list]
        self.assertTrue(all(len(message) <= 4096 for message in messages))
        self.assertEqual(
            messages[0].split('\n\n', 1)[1] + messages[1].split('\n\n', 1)[1],
            transcript,
        )

    def test_only_the_owner_can_approve_access_requests(self):
        self.store.request_user_access(321, 321, 'Alex', None, 'alex_example')
        callback = {
            'id': 'unauthorized-approval',
            'data': 'access|321',
            'from': {'id': 777},
            'message': {'chat': {'id': 777}},
        }
        with (
            patch('app._answer_callback') as answer_callback,
            patch('app._send_message') as send_message,
        ):
            bot_app._handle_callback_query(callback)

        self.assertFalse(self.store.user_is_approved(321))
        answer_callback.assert_called_once_with(
            'unauthorized-approval',
            'Only the owner can approve access.',
        )
        send_message.assert_not_called()

    def test_queue_limits_users_and_releases_slots_when_a_job_finishes(self):
        job_ids = []
        for index in range(4):
            job_id, created = self.store.create_job(
                800 + index,
                'https://youtu.be/dQw4w9WgXcQ',
                40 + index,
                40 + index,
            )
            self.assertTrue(created)
            job_ids.append(job_id)
            self.store.update_job(
                job_id,
                status='processing' if index == 0 else 'queued',
            )

        with self.assertRaises(bot_app.QueueAdmissionError) as error:
            self.store.create_job(
                804,
                'https://youtu.be/dQw4w9WgXcQ',
                44,
                44,
            )
        self.assertEqual(error.exception.reason, 'queue_full')

        _, created = self.store.create_job(
            806,
            'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
            43,
            43,
        )
        self.assertTrue(created)
        self.assertEqual(self.store.get_job(job_ids[3])['status'], 'cancelled')
        self.assertEqual(len(self.store.active_jobs()), 4)

        self.store.update_job(job_ids[0], status='completed')
        replacement_id, created = self.store.create_job(
            805,
            'https://youtu.be/dQw4w9WgXcQ',
            44,
            44,
        )
        self.assertTrue(created)
        self.assertNotEqual(replacement_id, job_ids[0])

    def test_new_link_replaces_unstarted_jobs(self):
        for index, status in enumerate(
            ('discovering', 'awaiting_language', 'queued')
        ):
            with self.subTest(status=status):
                user_id = 60 + index
                old_job_id, _ = self.store.create_job(
                    810 + index * 2,
                    'https://youtu.be/dQw4w9WgXcQ',
                    user_id,
                    user_id,
                )
                self.store.update_job(old_job_id, status=status)

                new_job_id, created = self.store.create_job(
                    811 + index * 2,
                    'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
                    user_id,
                    user_id,
                )

                self.assertTrue(created)
                self.assertEqual(self.store.get_job(old_job_id)['status'], 'cancelled')
                self.assertEqual(self.store.get_job(new_job_id)['status'], 'discovering')

                if status == 'queued':
                    with patch('app.process_video') as process_video:
                        bot_app._process_job(old_job_id)
                    process_video.assert_not_called()

    def test_new_link_replacement_is_silent_to_the_user(self):
        old_job_id, _ = self.store.create_job(
            818,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )
        update = {
            'update_id': 819,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': 'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
            },
        }

        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        self.assertEqual(self.store.get_job(old_job_id)['status'], 'cancelled')
        self.assertEqual(send_message.call_count, 1)
        self.assertIn('added to the queue', send_message.call_args.args[1])
        self.assertNotIn('cancel', send_message.call_args.args[1].casefold())
        submit_job.assert_called_once_with(
            bot_app._discover_job,
            self.store.active_jobs()[0]['job_id'],
        )

    def test_discovery_does_not_resurrect_a_replaced_job(self):
        job_id, _ = self.store.create_job(
            820,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )

        def replace_job_during_discovery(_url):
            self.store.create_job(
                821,
                'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
                42,
                42,
            )
            return {
                'title': 'Old video',
                'duration': 60,
                'tracks': [
                    {
                        'language_code': 'en',
                        'language_name': 'English',
                        'source': 'manual',
                    }
                ],
            }

        with (
            patch('app.inspect_video', side_effect=replace_job_during_discovery),
            patch('app._send_message') as send_message,
            patch('app._send_progress_message') as send_progress,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._discover_job(job_id)

        self.assertEqual(self.store.get_job(job_id)['status'], 'cancelled')
        send_message.assert_not_called()
        send_progress.assert_not_called()
        submit_job.assert_not_called()

    def test_user_cannot_replace_job_after_processing_starts(self):
        job_id, _ = self.store.create_job(
            830,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )
        self.store.update_job(job_id, status='processing')

        with self.assertRaises(bot_app.QueueAdmissionError) as error:
            self.store.create_job(
                831,
                'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
                42,
                42,
            )
        self.assertEqual(error.exception.reason, 'user_already_processing')
        self.assertEqual(self.store.get_job(job_id)['status'], 'processing')

        update = {
            'update_id': 833,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': 'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
            },
        }
        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        self.assertIn('already started processing', send_message.call_args.args[1])
        submit_job.assert_not_called()

        self.store.update_job(job_id, status='completed')
        _, created = self.store.create_job(
            832,
            'https://www.youtube.com/watch?v=M8qOjBw2Vo8',
            42,
            42,
        )
        self.assertTrue(created)

    def test_queue_position_counts_active_jobs_and_processing_jobs(self):
        first_job_id, _ = self.store.create_job(
            810,
            'https://youtu.be/dQw4w9WgXcQ',
            50,
            50,
        )
        second_job_id, _ = self.store.create_job(
            811,
            'https://youtu.be/dQw4w9WgXcQ',
            51,
            51,
        )
        language_job_id, _ = self.store.create_job(
            812,
            'https://youtu.be/dQw4w9WgXcQ',
            52,
            52,
        )
        self.store.update_job(first_job_id, status='processing')
        self.store.update_job(second_job_id, status='queued')
        self.store.update_job(language_job_id, status='awaiting_language')

        self.assertEqual(
            self.store.queue_position(second_job_id),
            {
                'position': 2,
                'ahead': 1,
                'active': 2,
                'processing': 1,
            },
        )
        self.assertEqual(
            bot_app._queue_status_message(second_job_id),
            '2/2\n\n',
        )
        self.assertIsNone(self.store.queue_position(language_job_id))
        self.store.update_job(first_job_id, status='completed')
        self.assertEqual(
            self.store.queue_position(second_job_id),
            {
                'position': 1,
                'ahead': 0,
                'active': 1,
                'processing': 0,
            },
        )
        self.assertEqual(bot_app._queue_status_message(second_job_id), '')

    def test_full_queue_sends_a_clear_message(self):
        for index in range(4):
            self.store.create_job(
                820 + index,
                'https://youtu.be/dQw4w9WgXcQ',
                50 + index,
                50 + index,
            )
        self._approve_user(54)
        update = {
            'update_id': 824,
            'message': {
                'from': {'id': 54},
                'chat': {'id': 54},
                'text': 'https://youtu.be/dQw4w9WgXcQ',
            },
        }
        with (
            patch('app._send_message') as send_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        self.assertIn('queue is full', send_message.call_args.args[1])
        submit_job.assert_not_called()

    def test_extracts_youtube_url_from_markdown_link(self):
        url = 'https://www.youtube.com/watch?v=M8qOjBw2Vo8'

        self.assertEqual(
            bot_app._youtube_url_from_text(f'[Video title]({url})'),
            url,
        )

    def test_uses_url_from_telegram_text_link_entity(self):
        url = 'https://www.youtube.com/watch?v=M8qOjBw2Vo8'
        update = {
            'update_id': 723,
            'message': {
                'from': {'id': 42},
                'chat': {'id': 42},
                'text': 'Watch this video',
                'entities': [
                    {
                        'type': 'text_link',
                        'offset': 0,
                        'length': 16,
                        'url': url,
                    }
                ],
            },
        }
        with (
            patch('app._send_message'),
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_update(update)

        job = self.store.get_job(self.store.active_jobs()[0]['job_id'])
        self.assertEqual(job['youtube_url'], url)
        submit_job.assert_called_once_with(bot_app._discover_job, job['job_id'])

    def test_language_callback_rejects_another_user(self):
        job_id, _ = self.store.create_job(
            721,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )
        self.store.update_job(
            job_id,
            status='awaiting_language',
            tracks=[
                {
                    'language_code': 'fr',
                    'language_name': 'French',
                    'source': 'manual',
                }
            ],
        )
        callback = {
            'id': 'callback-1',
            'data': f'lang|{job_id}|0',
            'from': {'id': 99},
            'message': {'chat': {'id': 42}},
        }
        with (
            patch('app._answer_callback') as answer_callback,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_callback_query(callback)

        self.assertEqual(self.store.get_job(job_id)['status'], 'awaiting_language')
        self.assertIn('another user', answer_callback.call_args.args[1])
        submit_job.assert_not_called()

    def test_discovery_explains_when_audio_language_is_unknown(self):
        job_id, _ = self.store.create_job(
            725,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )
        with (
            patch(
                'app.inspect_video',
                side_effect=bot_app.AudioLanguageError('language unavailable'),
            ),
            patch('app._send_message') as send_message,
        ):
            bot_app._discover_job(job_id)

        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'audio_language_unknown')
        self.assertIn("audio language", send_message.call_args.args[1])

    def test_language_callback_queues_selected_track(self):
        track = {
            'language_code': 'fr',
            'language_name': 'French',
            'source': 'manual',
        }
        job_id, _ = self.store.create_job(
            722,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )
        self.store.update_job(
            job_id,
            status='awaiting_language',
            tracks=[track],
        )
        callback = {
            'id': 'callback-2',
            'data': f'lang|{job_id}|0',
            'from': {'id': 42},
            'message': {'message_id': 123, 'chat': {'id': 42}},
        }
        with (
            patch('app._answer_callback'),
            patch('app._edit_message_text') as edit_message,
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_callback_query(callback)
            progress_callback = submit_job.call_args.args[2]
            self.assertTrue(progress_callback('Step 2/4: Preparing TinyASR...'))
            edit_message.assert_called_with(42, 123, 'Step 2/4: Preparing TinyASR...')
            self.assertNotIn(
                'Queue:',
                edit_message.call_args_list[0].args[2],
            )

        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(job['selected_track'], track)
        submit_job.assert_called_once()
        self.assertEqual(submit_job.call_args.args[:2], (bot_app._process_job, job_id))

    def test_process_job_delivers_audio_subtitles_transcript_and_reports(self):
        track = {
            'language_code': 'en',
            'language_name': 'English',
            'source': 'automatic',
        }
        job_id, _ = self.store.create_job(
            724,
            'https://youtu.be/dQw4w9WgXcQ',
            42,
            42,
        )
        self.store.update_job(
            job_id,
            status='queued',
            selected_track=track,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = [
                Path(temp_dir) / name
                for name in (
                    'audio.mp3',
                    'subtitles.vtt',
                    'tinyasr-transcript.txt',
                    'subtitle-differences.html',
                    'subtitle-alignment.json',
                )
            ]
            for path in paths:
                path.write_text('file', encoding='utf-8')
            result = {
                'title': 'Video',
                'wer': 10.0,
                'cer': 5.0,
                'difference_count': 1,
                'subtitle_cue_count': 10,
                'differences': [
                    {
                        'timestamp': '00:00:02.000',
                        'source': 'hello world',
                        'tinyasr_parts': [
                            {'text': 'hello', 'different': False},
                            {'text': 'there', 'different': True},
                        ],
                    }
                ],
                'audio_converted': False,
                'audio_path': paths[0],
                'subtitle_path': paths[1],
                'transcript_path': paths[2],
                'differences_html_path': paths[3],
                'alignment_path': paths[4],
            }

            delivery_order = []
            transcript_callback = lambda _text: True
            with (
                self.assertLogs('telegram_asr_bot', level='INFO') as completion_logs,
                patch.dict(
                    os.environ,
                    {
                        'BOT_OWNER': '99',
                        'BOT_REPORTS_PATH': str(Path(temp_dir) / 'saved-reports'),
                    },
                ),
                patch('app.process_video', return_value=result) as process_video,
                patch(
                    'app._transcript_draft_callback',
                    return_value=transcript_callback,
                ),
                patch(
                    'app._send_audio',
                    side_effect=lambda *args: delivery_order.append('audio')
                    or {'message_id': 123},
                ) as send_audio,
                patch(
                    'app._send_document',
                    side_effect=lambda *args: delivery_order.append('html'),
                ) as send_document,
                patch('app._send_media_group') as send_media_group,
                patch('app._send_message') as send_message,
            ):
                bot_app._process_job(job_id)
                process_video.assert_called_once_with(
                    'https://youtu.be/dQw4w9WgXcQ',
                    track,
                    ANY,
                    None,
                    transcript_callback=transcript_callback,
                )
                send_message.assert_called_once()
                self.assertEqual(send_message.call_args.args[0], 99)
                notification_keyboard = send_message.call_args.args[2]
                report_id = notification_keyboard['inline_keyboard'][0][0][
                    'callback_data'
                ].split('|', 1)[1]
                saved_report = self.store.get_report(report_id)
                self.assertEqual(saved_report['video_id'], 'dQw4w9WgXcQ')
                self.assertEqual(saved_report['track'], track)
                self.assertIn('WER: 10.00%', saved_report['summary'])
                self.assertIn('CER: 5.00%', saved_report['summary'])
                self.assertNotIn('Word Error Rate', saved_report['summary'])
                self.assertNotIn('Character Error Rate', saved_report['summary'])
                self.assertTrue(
                    all(
                        (Path(temp_dir) / 'saved-reports' / report_id / filename).is_file()
                        for filename in saved_report['files']
                    )
                )
                self.assertEqual(self.store.list_reports(42)[0]['report_id'], report_id)

        self.assertEqual(self.store.get_job(job_id)['status'], 'completed')
        self.assertIn(
            'Video comparison completed for job',
            '\n'.join(completion_logs.output),
        )
        send_audio.assert_called_once_with(42, paths[0], 'Video')
        send_document.assert_called_once_with(
            42,
            paths[3],
            'Open to see the differences',
            bot_app._report_details_keyboard(report_id),
        )
        send_media_group.assert_not_called()
        self.assertEqual(delivery_order, ['audio', 'html'])

    def test_existing_report_is_sent_again_and_added_to_the_new_users_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = self._save_sample_report(temp_dir)
            message = {
                'from': {'id': 99},
                'chat': {'id': 99},
                'text': 'https://youtu.be/dQw4w9WgXcQ',
            }
            with (
                patch.dict(
                    os.environ,
                    {'BOT_OWNER': '99', 'BOT_REPORTS_PATH': temp_dir},
                ),
                patch('app._send_message') as send_message,
                patch('app._send_audio') as send_audio,
                patch('app._send_document') as send_document,
                patch('app._submit_job') as submit_job,
            ):
                bot_app._handle_message(message, 900)
                bot_app._handle_message(
                    {
                        'from': {'id': 99},
                        'chat': {'id': 99},
                        'text': '/reports',
                    },
                    901,
                )

        self.assertIn('already exists', send_message.call_args_list[0].args[1])
        keyboard = send_message.call_args_list[0].args[2]
        self.assertEqual(
            keyboard['inline_keyboard'][0][0]['callback_data'],
            'again|dQw4w9WgXcQ',
        )
        self.assertEqual(send_audio.call_args.args[0], 99)
        self.assertEqual(send_document.call_args.args[0], 99)
        self.assertEqual(
            send_document.call_args.args[3],
            bot_app._report_details_keyboard(report['report_id']),
        )
        self.assertEqual(len(self.store.list_reports(99)), 1)
        self.assertIn(report['title'], send_message.call_args_list[1].args[1])
        self.assertEqual(
            send_message.call_args_list[1].args[2]['inline_keyboard'][0][0][
                'callback_data'
            ],
            f"report|{report['report_id']}",
        )
        submit_job.assert_not_called()

    def test_report_owner_button_sends_saved_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = self._save_sample_report(temp_dir)
            callback = {
                'id': 'callback-report',
                'data': f"report|{report['report_id']}",
                'from': {'id': 99},
                'message': {'chat': {'id': 99}},
            }
            with (
                patch.dict(
                    os.environ,
                    {'BOT_OWNER': '99', 'BOT_REPORTS_PATH': temp_dir},
                ),
                patch('app._answer_callback') as answer_callback,
                patch('app._send_audio') as send_audio,
                patch('app._send_document') as send_document,
            ):
                bot_app._handle_callback_query(callback)

        answer_callback.assert_called_once_with('callback-report', 'Sending saved report.')
        self.assertEqual(send_audio.call_args.args[0], 99)
        self.assertEqual(send_document.call_args.args[0], 99)

    def test_details_button_sends_full_diff_table_and_extra_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = self._save_sample_report(temp_dir)
            callback = {
                'id': 'callback-details',
                'data': f"details|{report['report_id']}",
                'from': {'id': 42},
                'message': {'chat': {'id': 42}},
            }
            with (
                patch.dict(
                    os.environ,
                    {'BOT_OWNER': '99', 'BOT_REPORTS_PATH': temp_dir},
                ),
                patch('app._answer_callback'),
                patch('app._telegram_call', return_value={'message_id': 123}) as call,
                patch('app._send_message') as send_message,
                patch('app._send_media_group') as send_media_group,
            ):
                bot_app._handle_callback_query(callback)

        payload = call.call_args.args[1]
        self.assertEqual(payload['rich_message']['blocks'][0]['type'], 'table')
        row = payload['rich_message']['blocks'][0]['cells'][1]
        self.assertEqual(
            row[0]['text'][1],
            {'type': 'marked', 'text': 'world'},
        )
        self.assertEqual(
            row[1]['text'][1],
            {'type': 'marked', 'text': 'there'},
        )
        self.assertIn('WER: 10.00%', send_message.call_args.args[1])
        self.assertEqual(send_media_group.call_args.args[1][0].name, 'source-subtitles.vtt')

    def test_rerun_button_queues_a_fresh_job(self):
        callback = {
            'id': 'callback-again',
            'data': 'again|dQw4w9WgXcQ',
            'from': {'id': 42},
            'message': {'chat': {'id': 42}},
        }
        with (
            patch('app._answer_callback') as answer_callback,
            patch('app._send_message'),
            patch('app._submit_job') as submit_job,
        ):
            bot_app._handle_callback_query(callback, update_id=902)

        job = self.store.get_job(self.store.active_jobs()[0]['job_id'])
        self.assertEqual(job['youtube_url'], 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        self.assertEqual(job['status'], 'discovering')
        submit_job.assert_called_once_with(bot_app._discover_job, job['job_id'])
        answer_callback.assert_called_once_with(
            'callback-again',
            'Starting a new comparison.',
        )

    def test_transcript_table_marks_differences_in_both_columns(self):
        aligned_rows = [
            {
                'source_parts': [
                    {'text': 'hello ', 'different': False},
                    {'text': 'world', 'different': True},
                ],
                'tinyasr_parts': [
                    {'text': 'hello ', 'different': False},
                    {'text': 'there', 'different': True},
                ],
            }
        ]
        with patch('app._telegram_call', return_value={'message_id': 124}) as call:
            sent_messages = bot_app._send_transcript_tables(
                42,
                'Video',
                aligned_rows,
            )

        self.assertEqual(sent_messages, [{'message_id': 124}])
        call.assert_called_once()
        method, payload = call.call_args.args
        self.assertEqual(method, 'sendRichMessage')
        table = payload['rich_message']['blocks'][0]
        self.assertEqual(table['type'], 'table')
        self.assertEqual(
            table['cells'][1][0]['text'],
            ['hello ', {'type': 'marked', 'text': 'world'}],
        )
        self.assertEqual(
            table['cells'][1][1]['text'],
            ['hello ', {'type': 'marked', 'text': 'there'}],
        )

    def test_transcript_table_splits_at_telegram_row_limit(self):
        aligned_rows = [
            {
                'source_parts': [{'text': 'source', 'different': True}],
                'tinyasr_parts': [{'text': 'result', 'different': True}],
            }
            for index in range(bot_app._MAX_RICH_TABLE_ROWS + 1)
        ]
        with patch('app._telegram_call', return_value={}) as call:
            bot_app._send_transcript_tables(
                42,
                'Video',
                aligned_rows,
            )

        self.assertEqual(call.call_count, 2)
        first_table = call.call_args_list[0].args[1]['rich_message']['blocks'][0]
        second_table = call.call_args_list[1].args[1]['rich_message']['blocks'][0]
        self.assertEqual(len(first_table['cells']), bot_app._MAX_RICH_TABLE_ROWS + 1)
        self.assertEqual(len(second_table['cells']), 2)
        self.assertIn('(part 1/2)', first_table['caption'])
        self.assertIn('(part 2/2)', second_table['caption'])

    def test_media_group_upload_streams_files_as_multipart(self):
        class FakeResponse:
            status = 200

            def read(self):
                return json.dumps({'ok': True, 'result': [{}, {}]}).encode('utf-8')

        class FakeConnection:
            def __init__(self):
                self.headers = {}
                self.body = bytearray()

            def putrequest(self, method, path):
                self.request = (method, path)

            def putheader(self, name, value):
                self.headers[name] = value

            def endheaders(self):
                pass

            def send(self, data):
                self.body.extend(data)

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_paths = [Path(temp_dir) / 'audio.mp3', Path(temp_dir) / 'result.txt']
            file_paths[0].write_bytes(b'audio bytes')
            file_paths[1].write_bytes(b'transcript bytes')
            with (
                patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token'}),
                patch('app.http.client.HTTPSConnection', return_value=connection),
            ):
                result = bot_app._send_media_group(42, file_paths, 'Complete')

        self.assertEqual(len(result), 2)
        self.assertEqual(connection.request, ('POST', '/bottest-token/sendMediaGroup'))
        self.assertEqual(int(connection.headers['Content-Length']), len(connection.body))
        self.assertIn(b'audio bytes', connection.body)
        self.assertIn(b'transcript bytes', connection.body)
        self.assertIn(b'attach://file0', connection.body)

    def test_audio_upload_uses_send_audio_for_inline_playback(self):
        class FakeResponse:
            status = 200

            def read(self):
                return json.dumps({'ok': True, 'result': {}}).encode('utf-8')

        class FakeConnection:
            def __init__(self):
                self.headers = {}
                self.body = bytearray()

            def putrequest(self, method, path):
                self.request = (method, path)

            def putheader(self, name, value):
                self.headers[name] = value

            def endheaders(self):
                pass

            def send(self, data):
                self.body.extend(data)

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / 'audio.mp3'
            audio_path.write_bytes(b'mp3 audio bytes')
            with (
                patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token'}),
                patch('app.http.client.HTTPSConnection', return_value=connection),
            ):
                result = bot_app._send_audio(42, audio_path, 'Video title')

        self.assertEqual(result, {})
        self.assertEqual(connection.request, ('POST', '/bottest-token/sendAudio'))
        self.assertIn(b'name="audio"; filename="audio.mp3"', connection.body)
        self.assertIn(b'name="title"\r\n\r\nVideo title', connection.body)
        self.assertIn(
            b'name="caption"\r\n\r\nExtracted audio file',
            connection.body,
        )
        self.assertIn(b'mp3 audio bytes', connection.body)

    def test_html_document_upload_includes_the_details_button(self):
        class FakeResponse:
            status = 200

            def read(self):
                return json.dumps({'ok': True, 'result': {}}).encode('utf-8')

        class FakeConnection:
            def __init__(self):
                self.headers = {}
                self.body = bytearray()

            def putrequest(self, method, path):
                self.request = (method, path)

            def putheader(self, name, value):
                self.headers[name] = value

            def endheaders(self):
                pass

            def send(self, data):
                self.body.extend(data)

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        connection = FakeConnection()
        keyboard = bot_app._report_details_keyboard('a' * 32)
        with tempfile.TemporaryDirectory() as temp_dir:
            html_path = Path(temp_dir) / 'report.html'
            html_path.write_text('<html>report</html>', encoding='utf-8')
            with (
                patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token'}),
                patch('app.http.client.HTTPSConnection', return_value=connection),
            ):
                bot_app._send_document(
                    42,
                    html_path,
                    'Open to see the differences',
                    keyboard,
                )

        self.assertEqual(connection.request, ('POST', '/bottest-token/sendDocument'))
        self.assertIn(b'name="document"; filename="report.html"', connection.body)
        self.assertIn(b'Open to see the differences', connection.body)
        self.assertIn(b'View additional details', connection.body)
        self.assertIn(b'details|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', connection.body)

    def test_update_offset_survives_store_operations(self):
        self.store.set_offset(723)

        self.assertEqual(self.store.get_offset(), 723)


if __name__ == '__main__':
    unittest.main()
