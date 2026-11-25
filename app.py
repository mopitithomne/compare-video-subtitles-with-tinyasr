import http.client
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from pipeline import AudioLanguageError, inspect_video, process_video
from validation import youtube_video_id


load_dotenv()
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger('telegram_asr_bot')

_JOB_ID_RE = re.compile(r'^[a-f0-9]{32}$')
_MAX_WAITING_USERS = 3
_ACTIVE_JOB_STATUSES = ('discovering', 'awaiting_language', 'queued', 'processing')
_REPLACEABLE_JOB_STATUSES = ('discovering', 'awaiting_language', 'queued')
_MAX_RICH_TABLE_ROWS = 450
_MAX_RICH_TABLE_TEXT_BYTES = 28_000
_store = None
_executor = None
_store_lock = threading.RLock()


class QueueAdmissionError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.connection.execute('PRAGMA busy_timeout = 30000')
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    update_id INTEGER NOT NULL UNIQUE,
                    youtube_url TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT,
                    duration REAL,
                    tracks TEXT,
                    selected_track TEXT,
                    error_code TEXT,
                    wer REAL,
                    cer REAL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    youtube_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    track TEXT NOT NULL,
                    wer REAL NOT NULL,
                    cer REAL NOT NULL,
                    difference_count INTEGER NOT NULL,
                    subtitle_cue_count INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    files TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS reports_video_created
                ON reports (video_id, created_at DESC)
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS report_access (
                    report_id TEXT NOT NULL REFERENCES reports(report_id),
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (report_id, user_id)
                )
                '''
            )
            self.connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS report_access_user
                ON report_access (user_id, report_id)
                '''
            )
            self.connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS user_access (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    username TEXT,
                    status TEXT NOT NULL,
                    owner_notified_at TEXT,
                    approved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            self.connection.commit()

    def get_offset(self):
        with self.lock:
            row = self.connection.execute(
                "SELECT value FROM bot_state WHERE key = 'update_offset'"
            ).fetchone()
        return int(row['value']) if row else 0

    def set_offset(self, offset):
        with self.lock:
            self.connection.execute(
                '''
                INSERT INTO bot_state (key, value)
                VALUES ('update_offset', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                ''',
                (str(offset),),
            )
            self.connection.commit()

    def create_job(self, update_id, youtube_url, user_id, chat_id):
        job_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f'telegram-bot-api:{update_id}',
        ).hex
        now = _now()
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                existing = self.connection.execute(
                    'SELECT job_id FROM jobs WHERE update_id = ?',
                    (update_id,),
                ).fetchone()
                if existing is not None:
                    self.connection.commit()
                    return existing['job_id'], False

                processing_job = self.connection.execute(
                    '''
                    SELECT 1 FROM jobs
                    WHERE user_id = ? AND status = 'processing'
                    LIMIT 1
                    ''',
                    (user_id,),
                ).fetchone()
                if processing_job is not None:
                    raise QueueAdmissionError('user_already_processing')

                replaceable_placeholders = ', '.join(
                    '?' for _ in _REPLACEABLE_JOB_STATUSES
                )
                replaced_jobs = self.connection.execute(
                    f'''
                    SELECT job_id FROM jobs
                    WHERE user_id = ? AND status IN ({replaceable_placeholders})
                    ''',
                    (user_id, *_REPLACEABLE_JOB_STATUSES),
                ).fetchall()
                if replaced_jobs:
                    self.connection.execute(
                        f'''
                        UPDATE jobs
                        SET status = 'cancelled',
                            error_code = 'superseded_by_new_link',
                            completed_at = ?,
                            updated_at = ?
                        WHERE user_id = ? AND status IN ({replaceable_placeholders})
                        ''',
                        (
                            now,
                            now,
                            user_id,
                            *_REPLACEABLE_JOB_STATUSES,
                        ),
                    )

                status_placeholders = ', '.join(
                    '?' for _ in _ACTIVE_JOB_STATUSES
                )
                active_users = self.connection.execute(
                    f'''
                    SELECT COUNT(DISTINCT user_id) AS count FROM jobs
                    WHERE status IN ({status_placeholders})
                    ''',
                    _ACTIVE_JOB_STATUSES,
                ).fetchone()['count']
                if active_users >= _MAX_WAITING_USERS + 1:
                    raise QueueAdmissionError('queue_full')

                cursor = self.connection.execute(
                    '''
                    INSERT INTO jobs
                        (job_id, update_id, youtube_url, user_id, chat_id, status,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'discovering', ?, ?)
                    ''',
                    (job_id, update_id, youtube_url, user_id, chat_id, now, now),
                )
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

            for replaced_job in replaced_jobs:
                logger.info(
                    'Silently cancelled pending job %s after a newer link arrived.',
                    replaced_job['job_id'],
                )
        return job_id, cursor.rowcount == 1

    def get_job(self, job_id):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM jobs WHERE job_id = ?',
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        job = dict(row)
        for field in ('tracks', 'selected_track'):
            if job[field] is not None:
                job[field] = json.loads(job[field])
        return job

    def update_job(self, job_id, expected_status=None, **updates):
        allowed_fields = {
            'status',
            'title',
            'duration',
            'tracks',
            'selected_track',
            'error_code',
            'wer',
            'cer',
            'completed_at',
        }
        if not updates or not set(updates).issubset(allowed_fields):
            raise ValueError('Unsupported or empty job update.')
        values = {}
        for field, value in updates.items():
            if field in {'tracks', 'selected_track'} and value is not None:
                value = json.dumps(value, ensure_ascii=False)
            values[field] = value
        values['updated_at'] = _now()
        assignments = ', '.join(f'{field} = ?' for field in values)
        where = 'job_id = ?'
        parameters = (*values.values(), job_id)
        if expected_status is not None:
            where += ' AND status = ?'
            parameters += (expected_status,)
        with self.lock:
            cursor = self.connection.execute(
                f'UPDATE jobs SET {assignments} WHERE {where}',
                parameters,
            )
            self.connection.commit()
        return cursor.rowcount == 1

    def active_jobs(self):
        with self.lock:
            rows = self.connection.execute(
                f'''
                SELECT job_id, status FROM jobs
                WHERE status IN ({', '.join('?' for _ in _ACTIVE_JOB_STATUSES)})
                ORDER BY created_at ASC, rowid ASC
                ''',
                _ACTIVE_JOB_STATUSES,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_report(self, report, user_id):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                self.connection.execute(
                    '''
                    INSERT INTO reports
                        (report_id, video_id, youtube_url, title, track, wer, cer,
                         difference_count, subtitle_cue_count, summary, files,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        report['report_id'],
                        report['video_id'],
                        report['youtube_url'],
                        report['title'],
                        json.dumps(report['track'], ensure_ascii=False),
                        report['wer'],
                        report['cer'],
                        report['difference_count'],
                        report['subtitle_cue_count'],
                        report['summary'],
                        json.dumps(report['files'], ensure_ascii=False),
                        report['created_at'],
                    ),
                )
                self.connection.execute(
                    '''
                    INSERT INTO report_access (report_id, user_id)
                    VALUES (?, ?)
                    ''',
                    (report['report_id'], user_id),
                )
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def get_report(self, report_id):
        with self.lock:
            row = self.connection.execute(
                'SELECT * FROM reports WHERE report_id = ?',
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        report = dict(row)
        report['track'] = json.loads(report['track'])
        report['files'] = json.loads(report['files'])
        return report

    def find_report_for_video(self, video_id):
        with self.lock:
            row = self.connection.execute(
                '''
                SELECT report_id FROM reports
                WHERE video_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                ''',
                (video_id,),
            ).fetchone()
        return self.get_report(row['report_id']) if row is not None else None

    def add_report_access(self, report_id, user_id):
        with self.lock:
            self.connection.execute(
                '''
                INSERT OR IGNORE INTO report_access (report_id, user_id)
                VALUES (?, ?)
                ''',
                (report_id, user_id),
            )
            self.connection.commit()

    def user_can_access_report(self, report_id, user_id):
        with self.lock:
            row = self.connection.execute(
                '''
                SELECT 1 FROM report_access
                WHERE report_id = ? AND user_id = ?
                ''',
                (report_id, user_id),
            ).fetchone()
        return row is not None

    def list_reports(self, user_id, limit=10):
        with self.lock:
            rows = self.connection.execute(
                '''
                SELECT reports.report_id, reports.title, reports.track,
                       reports.wer, reports.cer, reports.created_at
                FROM reports
                JOIN report_access USING (report_id)
                WHERE report_access.user_id = ?
                ORDER BY reports.created_at DESC, reports.rowid DESC
                LIMIT ?
                ''',
                (user_id, limit),
            ).fetchall()
        reports = [dict(row) for row in rows]
        for report in reports:
            report['track'] = json.loads(report['track'])
        return reports

    def request_user_access(self, user_id, chat_id, first_name, last_name, username):
        now = _now()
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                row = self.connection.execute(
                    'SELECT status, owner_notified_at FROM user_access WHERE user_id = ?',
                    (user_id,),
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        '''
                        INSERT INTO user_access
                            (user_id, chat_id, first_name, last_name, username,
                             status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                        ''',
                        (
                            user_id,
                            chat_id,
                            first_name,
                            last_name,
                            username,
                            now,
                            now,
                        ),
                    )
                    status = 'pending'
                    notify_owner = True
                else:
                    status = row['status']
                    notify_owner = (
                        status == 'pending' and row['owner_notified_at'] is None
                    )
                    self.connection.execute(
                        '''
                        UPDATE user_access
                        SET chat_id = ?, first_name = ?, last_name = ?,
                            username = ?, updated_at = ?
                        WHERE user_id = ?
                        ''',
                        (
                            chat_id,
                            first_name,
                            last_name,
                            username,
                            now,
                            user_id,
                        ),
                    )
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
        return {'status': status, 'notify_owner': notify_owner}

    def mark_access_request_notified(self, user_id):
        now = _now()
        with self.lock:
            self.connection.execute(
                '''
                UPDATE user_access
                SET owner_notified_at = ?, updated_at = ?
                WHERE user_id = ? AND status = 'pending'
                ''',
                (now, now, user_id),
            )
            self.connection.commit()

    def approve_user(self, user_id):
        now = _now()
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                row = self.connection.execute(
                    '''
                    SELECT user_id, chat_id, first_name, last_name, username, status
                    FROM user_access WHERE user_id = ?
                    ''',
                    (user_id,),
                ).fetchone()
                if row is None:
                    self.connection.commit()
                    return None
                if row['status'] != 'approved':
                    self.connection.execute(
                        '''
                        UPDATE user_access
                        SET status = 'approved', approved_at = ?, updated_at = ?
                        WHERE user_id = ?
                        ''',
                        (now, now, user_id),
                    )
                self.connection.commit()
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
        approved_user = dict(row)
        approved_user['status'] = 'approved'
        return approved_user

    def user_is_approved(self, user_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM user_access WHERE user_id = ? AND status = 'approved'",
                (user_id,),
            ).fetchone()
        return row is not None

    def close(self):
        with self.lock:
            self.connection.close()


def _required_environment(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


def _owner_chat_id():
    value = _required_environment('BOT_OWNER')
    try:
        chat_id = int(value)
    except ValueError:
        raise RuntimeError('BOT_OWNER must be a positive Telegram user ID.') from None
    if chat_id <= 0:
        raise RuntimeError('BOT_OWNER must be a positive Telegram user ID.')
    return chat_id


def _reports_directory():
    configured_path = os.environ.get('BOT_REPORTS_PATH')
    if configured_path:
        return Path(configured_path)
    database_path = os.environ.get('BOT_DATABASE_PATH', 'data/bot.sqlite3')
    return Path(database_path).parent / 'reports'


def _get_store():
    if _store is None:
        raise RuntimeError('The local job store has not been initialized.')
    return _store


def _telegram_call(method, payload=None, timeout=30):
    token = _required_environment('TELEGRAM_BOT_TOKEN')
    request_data = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload or {}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request_data, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Telegram API returned HTTP {error.code}.') from None
    except urllib.error.URLError as error:
        raise RuntimeError(f'Telegram API request failed: {error.reason}.') from None
    if not isinstance(data, dict) or not data.get('ok'):
        description = data.get('description', 'unknown error') if isinstance(data, dict) else 'invalid response'
        raise RuntimeError(f'Telegram API rejected {method}: {description}.')
    return data.get('result')


def _send_message(chat_id, text, reply_markup=None):
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return _telegram_call('sendMessage', payload)


def _rich_text_parts(parts, style):
    rich_parts = []
    for part in parts:
        if part['different']:
            rich_parts.append({'type': style, 'text': part['text']})
        else:
            rich_parts.append(part['text'])
    return rich_parts or ''


def _send_transcript_tables(chat_id, title, aligned_rows):
    chunks = []
    current_chunk = []
    current_text_bytes = 0
    for row in aligned_rows:
        row_text_bytes = sum(
            len(part['text'].encode('utf-8'))
            for side in ('source_parts', 'tinyasr_parts')
            for part in row[side]
        )
        if row_text_bytes + 1024 > _MAX_RICH_TABLE_TEXT_BYTES:
            raise ValueError(
                'A transcript row is too large for a Telegram rich message.'
            )
        if current_chunk and (
            len(current_chunk) >= _MAX_RICH_TABLE_ROWS
            or current_text_bytes + row_text_bytes + 1024 > _MAX_RICH_TABLE_TEXT_BYTES
        ):
            chunks.append(current_chunk)
            current_chunk = []
            current_text_bytes = 0
        current_chunk.append(row)
        current_text_bytes += row_text_bytes
    if current_chunk:
        chunks.append(current_chunk)

    sent_messages = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        caption = f'{title} — full transcript comparison'
        if len(chunks) > 1:
            caption += f' (part {chunk_index}/{len(chunks)})'

        cells = [[
            {'text': 'YouTube subtitles (differences marked)', 'is_header': True},
            {'text': 'TinyASR (differences marked)', 'is_header': True},
        ]]
        for row in chunk:
            cells.append(
                [
                    {
                        'text': _rich_text_parts(
                            row['source_parts'],
                            'marked',
                        )
                    },
                    {
                        'text': _rich_text_parts(
                            row['tinyasr_parts'],
                            'marked',
                        )
                    },
                ]
            )

        table = {
            'type': 'table',
            'is_bordered': True,
            'is_compact': True,
            'caption': caption,
            'cells': cells,
        }
        payload = {
            'chat_id': chat_id,
            'rich_message': {'blocks': [table]},
        }
        sent_messages.append(_telegram_call('sendRichMessage', payload))
    return sent_messages


def _edit_message_text(chat_id, message_id, text, reply_markup=None):
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return _telegram_call('editMessageText', payload)


def _upload_multipart(method, chat_id, fields, file_parts):
    boundary = f'----TelegramUpload{uuid.uuid4().hex}'
    def field(name, value):
        return (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'
        ).encode('utf-8')

    form_fields = [field('chat_id', chat_id)]
    form_fields.extend(field(name, value) for name, value in fields)
    files = [(name, Path(file_path)) for name, file_path in file_parts]
    file_headers = []
    for name, file_path in files:
        content_type = mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream'
        file_headers.append(
            (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{file_path.name}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n'
            ).encode('utf-8')
        )
    ending = f'--{boundary}--\r\n'.encode('ascii')
    content_length = (
        sum(map(len, form_fields))
        + sum(
            len(header) + file_path.stat().st_size + 2
            for header, (_, file_path) in zip(file_headers, files)
        )
        + len(ending)
    )
    token = _required_environment('TELEGRAM_BOT_TOKEN')
    connection = http.client.HTTPSConnection('api.telegram.org', timeout=300)
    try:
        connection.putrequest('POST', f'/bot{token}/{method}')
        connection.putheader('Content-Type', f'multipart/form-data; boundary={boundary}')
        connection.putheader('Content-Length', str(content_length))
        connection.endheaders()
        for field_data in form_fields:
            connection.send(field_data)
        for header, (_, file_path) in zip(file_headers, files):
            connection.send(header)
            with file_path.open('rb') as upload_file:
                while chunk := upload_file.read(1024 * 1024):
                    connection.send(chunk)
            connection.send(b'\r\n')
        connection.send(ending)
        response = connection.getresponse()
        try:
            data = json.loads(response.read().decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError('Telegram returned an invalid file-upload response.') from None
        if response.status >= 400:
            raise RuntimeError(f'Telegram API returned HTTP {response.status}.')
        if not isinstance(data, dict) or not data.get('ok'):
            description = (
                data.get('description', 'unknown error')
                if isinstance(data, dict)
                else 'invalid response'
            )
            raise RuntimeError(f'Telegram API rejected file uploads: {description}.')
        return data.get('result')
    except (OSError, http.client.HTTPException) as error:
        raise RuntimeError(f'Telegram file upload failed: {error}.') from None
    finally:
        connection.close()


def _send_audio(chat_id, audio_path, title):
    result = _upload_multipart(
        'sendAudio',
        chat_id,
        [('title', title)],
        [('audio', audio_path)],
    )
    if not isinstance(result, dict):
        raise RuntimeError('Telegram did not confirm receipt of the audio file.')
    return result


def _send_document(chat_id, document_path, caption, reply_markup=None):
    fields = [('caption', caption)]
    if reply_markup is not None:
        fields.append(
            ('reply_markup', json.dumps(reply_markup, ensure_ascii=False))
        )
    result = _upload_multipart(
        'sendDocument',
        chat_id,
        fields,
        [('document', document_path)],
    )
    if not isinstance(result, dict):
        raise RuntimeError('Telegram did not confirm receipt of the document.')
    return result


def _send_media_group(chat_id, file_paths, caption):
    files = [Path(file_path) for file_path in file_paths]
    if not 2 <= len(files) <= 10:
        raise ValueError('Telegram media groups must contain between 2 and 10 files.')
    media = []
    file_parts = []
    for index, file_path in enumerate(files):
        item = {'type': 'document', 'media': f'attach://file{index}'}
        if index == 0:
            item['caption'] = caption
        media.append(item)
        file_parts.append((f'file{index}', file_path))

    result = _upload_multipart(
        'sendMediaGroup',
        chat_id,
        [('media', json.dumps(media, ensure_ascii=False))],
        file_parts,
    )
    if not isinstance(result, list) or len(result) != len(files):
        raise RuntimeError('Telegram did not confirm receipt of every result file.')
    return result


def _report_view_keyboard(report_id):
    return {
        'inline_keyboard': [[
            {
                'text': 'View report',
                'callback_data': f'report|{report_id}',
            }
        ]]
    }


def _report_rerun_keyboard(video_id):
    return {
        'inline_keyboard': [[
            {
                'text': 'Run this video again',
                'callback_data': f'again|{video_id}',
            }
        ]]
    }


def _report_details_keyboard(report_id):
    return {
        'inline_keyboard': [[
            {
                'text': 'Voir les autres informations',
                'callback_data': f'details|{report_id}',
            }
        ]]
    }


def _archive_report(job, result, track, summary):
    report_id = uuid.uuid4().hex
    report_directory = _reports_directory() / report_id
    report_directory.mkdir(parents=True, exist_ok=False)
    subtitle_suffix = Path(result['subtitle_path']).suffix.casefold()
    if subtitle_suffix not in {'.srt', '.vtt', '.txt'}:
        subtitle_suffix = '.txt'
    files = [
        'audio.mp3',
        f'source-subtitles{subtitle_suffix}',
        'tinyasr-transcript.txt',
        'subtitle-differences.html',
        'subtitle-alignment.json',
    ]
    source_files = (
        result['audio_path'],
        result['subtitle_path'],
        result['transcript_path'],
        result['differences_html_path'],
        result['alignment_path'],
    )
    saved = False
    try:
        for source_path, filename in zip(source_files, files):
            shutil.copy2(source_path, report_directory / filename)
        video_id = youtube_video_id(job['youtube_url'])
        if video_id is None:
            raise RuntimeError('The completed report has an invalid YouTube video ID.')
        report = {
            'report_id': report_id,
            'video_id': video_id,
            'youtube_url': job['youtube_url'],
            'title': result['title'],
            'track': track,
            'wer': result['wer'],
            'cer': result['cer'],
            'difference_count': result['difference_count'],
            'subtitle_cue_count': result['subtitle_cue_count'],
            'summary': summary,
            'files': files,
            'created_at': _now(),
        }
        _get_store().save_report(report, job['user_id'])
        saved = True
        return report
    finally:
        if not saved and report_directory.exists():
            shutil.rmtree(report_directory)


def _saved_report_paths(report):
    report_directory = _reports_directory() / report['report_id']
    filenames = report['files']
    if (
        not isinstance(filenames, list)
        or len(filenames) != 5
        or any(not isinstance(name, str) or Path(name).name != name for name in filenames)
    ):
        raise RuntimeError('The saved report contains invalid file references.')
    paths = [report_directory / filename for filename in filenames]
    if not all(path.is_file() for path in paths):
        raise RuntimeError('One or more saved report files are missing.')
    return paths


def _send_saved_report(chat_id, report):
    audio_path, _, _, html_path, _ = _saved_report_paths(report)
    title = report['title'][:64]
    _send_audio(chat_id, audio_path, title)
    _send_document(
        chat_id,
        html_path,
        'HTML comparison report',
        _report_details_keyboard(report['report_id']),
    )


def _send_report_details(chat_id, report):
    _, subtitles_path, transcript_path, _, alignment_path = _saved_report_paths(
        report
    )
    try:
        aligned_rows = json.loads(alignment_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError('The saved transcript comparison is unreadable.') from None
    if not isinstance(aligned_rows, list) or not aligned_rows:
        raise RuntimeError('The saved transcript comparison is invalid.')
    _send_transcript_tables(chat_id, report['title'], aligned_rows)
    _send_message(chat_id, report['summary'])
    _send_media_group(
        chat_id,
        [subtitles_path, transcript_path],
        'Source subtitles and TinyASR transcript',
    )


def _notify_owner(report, requester_id):
    message = (
        f"New report completed: {report['title']}\n"
        f"Requested by Telegram user {requester_id}\n"
        f"WER: {report['wer']:.2f}% | CER: {report['cer']:.2f}%"
    )
    try:
        _send_message(
            _owner_chat_id(),
            message,
            _report_view_keyboard(report['report_id']),
        )
    except RuntimeError:
        logger.exception('Could not notify the owner about report %s.', report['report_id'])


def _progress_callback(chat_id, message_id):
    def update_progress(text):
        try:
            _edit_message_text(chat_id, message_id, text)
        except RuntimeError:
            logger.exception('Could not update progress message %s.', message_id)
            return False
        return True

    return update_progress


def _send_progress_message(chat_id, text):
    response = _send_message(chat_id, text)
    message_id = response.get('message_id') if isinstance(response, dict) else None
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise RuntimeError('Telegram did not return a valid progress message ID.')
    return _progress_callback(chat_id, message_id)


def _start_progress_message(chat_id, message_id, text):
    if not isinstance(message_id, bool) and isinstance(message_id, int):
        try:
            _edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup={'inline_keyboard': []},
            )
        except RuntimeError:
            logger.exception(
                'Could not edit the language-selection message; sending a new progress message.'
            )
        else:
            return _progress_callback(chat_id, message_id)
    return _send_progress_message(chat_id, text)


def _answer_callback(callback_id, text):
    return _telegram_call(
        'answerCallbackQuery',
        {'callback_query_id': callback_id, 'text': text},
    )


def _submit_job(function, job_id, *args):
    if _executor is None:
        raise RuntimeError('The background job executor has not been initialized.')
    logger.info('Queuing background task %s for job %s.', function.__name__, job_id)
    future = _executor.submit(function, job_id, *args)

    def report_failure(completed_future: Future):
        try:
            completed_future.result()
        except Exception:
            logger.exception('Background task %s failed for job %s.', function.__name__, job_id)

    future.add_done_callback(report_failure)


def _fail_job(
    job_id,
    error_code,
    message,
    progress_callback=None,
    expected_status=None,
):
    store = _get_store()
    job = store.get_job(job_id)
    if job is None or not store.update_job(
        job_id,
        expected_status=expected_status,
        status='failed',
        error_code=error_code,
    ):
        return
    logger.warning('Marking job %s as failed (%s).', job_id, error_code)
    if progress_callback is not None and progress_callback(message):
        return
    _send_message(job['chat_id'], message)


def _language_keyboard(job_id, tracks):
    return {
        'inline_keyboard': [
            [
                {
                    'text': (
                        f"{track['language_name']} "
                        f"({'manual' if track['source'] == 'manual' else 'automatic'})"
                    ),
                    'callback_data': f'lang|{job_id}|{index}',
                }
            ]
            for index, track in enumerate(tracks)
        ]
    }


def _send_language_prompt(job_id):
    job = _get_store().get_job(job_id)
    if job is None or job['status'] != 'awaiting_language':
        return
    logger.info(
        'Waiting for language selection for job %s (%d subtitle tracks).',
        job_id,
        len(job['tracks']),
    )
    _send_message(
        job['chat_id'],
        f"Several TinyASR-compatible subtitle tracks are available for "
        f"\"{job['title']}\". Choose one:",
        _language_keyboard(job_id, job['tracks']),
    )


def _discover_job(job_id):
    job = _get_store().get_job(job_id)
    if job is None or job['status'] != 'discovering':
        return
    logger.info('Starting subtitle discovery for job %s.', job_id)
    try:
        details = inspect_video(job['youtube_url'])
    except Exception as error:
        latest_job = _get_store().get_job(job_id)
        if latest_job is None or latest_job['status'] != 'discovering':
            return
        logger.exception('Subtitle discovery failed for job %s.', job_id)
        if isinstance(error, AudioLanguageError):
            error_code = 'audio_language_unknown'
            error_message = (
                "Could not identify the video's audio language, so matching subtitles "
                'cannot be selected.'
            )
        else:
            error_code = type(error).__name__
            error_message = "I couldn't access this video. Check the URL and try again."
        _fail_job(
            job_id,
            error_code,
            error_message,
            expected_status='discovering',
        )
        return

    tracks = details['tracks']
    logger.info(
        'Subtitle discovery found %d compatible track(s) for job %s.',
        len(tracks),
        job_id,
    )
    if not tracks:
        _fail_job(
            job_id,
            'no_supported_subtitles',
            "No TinyASR-compatible subtitles in the video's audio language were found.",
            expected_status='discovering',
        )
        return

    if len(tracks) == 1:
        track = tracks[0]
        if not _get_store().update_job(
            job_id,
            expected_status='discovering',
            status='queued',
            title=details['title'],
            duration=details['duration'],
            tracks=tracks,
            selected_track=track,
        ):
            return
        job = _get_store().get_job(job_id)
        logger.info(
            'Automatically selected %s subtitles for job %s.',
            track['language_name'],
            job_id,
        )
        progress_callback = None
        try:
            progress_callback = _send_progress_message(
                job['chat_id'],
                f"Only one compatible subtitle language is available: "
                f"{track['language_name']}.\n\n"
                'Step 1/5: Preparing to download audio and subtitles...',
            )
        finally:
            _submit_job(_process_job, job_id, progress_callback)
        return

    if not _get_store().update_job(
        job_id,
        expected_status='discovering',
        status='awaiting_language',
        title=details['title'],
        duration=details['duration'],
        tracks=tracks,
    ):
        return
    _send_language_prompt(job_id)


def _process_job(job_id, progress_callback=None):
    job = _get_store().get_job(job_id)
    if job is None or job['status'] != 'queued':
        return
    track = job.get('selected_track')
    if not isinstance(track, dict):
        _fail_job(
            job_id,
            'missing_track',
            'The selected subtitle track could not be found. Send the video URL again.',
            progress_callback,
            expected_status='queued',
        )
        return

    if not _get_store().update_job(
        job_id,
        expected_status='queued',
        status='processing',
    ):
        return
    logger.info('Starting video processing for job %s.', job_id)
    if progress_callback is not None:
        progress_callback('Step 1/5: Downloading audio and selected subtitles...')
    try:
        with tempfile.TemporaryDirectory(prefix='telegram-asr-') as output_dir:
            result = process_video(
                job['youtube_url'],
                track,
                output_dir,
                progress_callback,
            )
            source_label = 'manual' if track['source'] == 'manual' else 'automatic'
            display_title = result['title'][:180]
            if len(result['title']) > 180:
                display_title += '...'
            result_message = (
                f'Comparison complete: {display_title}\n'
                f"Subtitle language: {track['language_name']} ({source_label})\n"
                f"WER (Word Error Rate): {result['wer']:.2f}%\n"
                f"CER (Character Error Rate): {result['cer']:.2f}%\n"
                f"Differing subtitle cues: {result['difference_count']} / "
                f"{result['subtitle_cue_count']}\n"
            )
            if result['audio_converted']:
                result_message += '\nThe audio attachment was converted to MP3 for Telegram playback.'
            if track['source'] == 'automatic':
                result_message += '\nNote: Automatic subtitles may be inaccurate.'
            report = _archive_report(job, result, track, result_message)
            _notify_owner(report, job['user_id'])
            if progress_callback is not None:
                progress_callback('Step 5/5: Sending audio, subtitles and results...')
            logger.info('Sending audio, subtitles and comparison files for job %s.', job_id)
            _send_audio(
                job['chat_id'],
                result['audio_path'],
                display_title[:64],
            )
            _send_document(
                job['chat_id'],
                result['differences_html_path'],
                'HTML comparison report',
                _report_details_keyboard(report['report_id']),
            )
    except Exception as error:
        logger.exception('Processing or result delivery failed for job %s.', job_id)
        if isinstance(error, AudioLanguageError):
            failure_message = (
                'Could not verify an audio track matching the selected subtitle language.'
            )
        else:
            failure_message = (
                'Processing or result delivery failed. Check the video and try again.'
            )
        _fail_job(
            job_id,
            type(error).__name__,
            failure_message,
            progress_callback,
            expected_status='processing',
        )
        return

    _get_store().update_job(
        job_id,
        status='completed',
        title=result['title'],
        wer=result['wer'],
        cer=result['cer'],
        completed_at=_now(),
    )
    completion_message = 'Comparison complete. Your audio and HTML report are attached.'
    if progress_callback is not None and not progress_callback(completion_message):
        _send_message(job['chat_id'], completion_message)


def _valid_telegram_ids(user_id, chat_id):
    return (
        not isinstance(user_id, bool)
        and isinstance(user_id, int)
        and user_id > 0
        and not isinstance(chat_id, bool)
        and isinstance(chat_id, int)
        and chat_id != 0
    )


def _youtube_url_from_text(text, entities=None):
    candidates = []
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict) or entity.get('type') != 'text_link':
                continue
            url = entity.get('url')
            if isinstance(url, str):
                candidates.append(url)
    candidates.extend(re.findall(r'https?://\S+', text, re.IGNORECASE))

    for candidate in candidates:
        candidate = candidate.rstrip('),.!?;]}>')
        if youtube_video_id(candidate):
            return candidate
    return None


def _send_help(chat_id):
    _send_message(
        chat_id,
        'Send a YouTube video URL to compare its audio transcription with the subtitles. '
        'If multiple compatible subtitle tracks are available, you can choose one.\n\n'
        'WER (Word Error Rate) is the number of word substitutions, deletions, '
        'and insertions divided by the number of words in the subtitles.\n'
        'CER (Character Error Rate) uses the same calculation at the character '
        'level, divided by the number of characters in the subtitles.\n'
        'Lower scores indicate a closer match; 0% means an exact match.\n'
        'Use /reports to view your saved reports.\n'
        'Only one video is processed at a time. Up to three other users can wait, '
        'and each user can have one queued or processing request.\n'
        'A new link replaces your pending request; once processing starts, you must '
        'wait for it to finish.',
    )


def _send_reports(chat_id, user_id):
    reports = _get_store().list_reports(user_id)
    if not reports:
        _send_message(chat_id, "You don't have any saved reports yet.")
        return
    lines = ['Your most recent saved reports:']
    buttons = []
    for index, report in enumerate(reports, start=1):
        language = report['track'].get('language_name', 'Unknown language')
        lines.append(
            f"{index}. {report['title']} ({language}, "
            f"WER {report['wer']:.2f}%, CER {report['cer']:.2f}%)"
        )
        buttons.append(
            [{
                'text': f"{report['title'][:42]} - WER {report['wer']:.1f}%",
                'callback_data': f"report|{report['report_id']}",
            }]
        )
    _send_message(
        chat_id,
        '\n'.join(lines),
        {'inline_keyboard': buttons},
    )


def _send_existing_report(chat_id, user_id, video_id, report):
    _get_store().add_report_access(report['report_id'], user_id)
    _send_message(
        chat_id,
        f"A saved report already exists for \"{report['title']}\". "
        'Sending it now. Tap below to run the video again.',
        _report_rerun_keyboard(video_id),
    )
    _send_saved_report(chat_id, report)


def _request_user_approval(user, user_id, chat_id):
    if user_id == _owner_chat_id():
        return True
    first_name = user.get('first_name')
    if not isinstance(first_name, str) or not first_name.strip():
        first_name = 'Unknown'
    last_name = user.get('last_name')
    if not isinstance(last_name, str):
        last_name = None
    username = user.get('username')
    if not isinstance(username, str):
        username = None

    access = _get_store().request_user_access(
        user_id,
        chat_id,
        first_name,
        last_name,
        username,
    )
    if access['status'] == 'approved':
        return True
    if access['notify_owner']:
        display_name = ' '.join(part for part in (first_name, last_name) if part)
        username_label = f'@{username}' if username else 'Not set'
        try:
            _send_message(
                _owner_chat_id(),
                f'New bot access request\n'
                f'Name: {display_name}\n'
                f'Username: {username_label}\n'
                f'Telegram ID: {user_id}',
                {
                    'inline_keyboard': [[
                        {
                            'text': 'Approve access',
                            'callback_data': f'access|{user_id}',
                        }
                    ]]
                },
            )
        except RuntimeError:
            logger.exception(
                'Could not notify the owner about access request from user %s.',
                user_id,
            )
        else:
            _get_store().mark_access_request_notified(user_id)

    _send_message(
        chat_id,
        'Access to this bot requires owner approval. '
        'Your request is pending; you will receive a message if approved.',
    )
    return False


def _handle_message(message, update_id):
    text = message.get('text')
    user = message.get('from')
    chat = message.get('chat')
    if not isinstance(text, str) or not isinstance(user, dict) or not isinstance(chat, dict):
        return

    user_id = user.get('id')
    chat_id = chat.get('id')
    if not _valid_telegram_ids(user_id, chat_id):
        return
    if not _request_user_approval(user, user_id, chat_id):
        return

    words = text.strip().split(maxsplit=1)
    command = words[0].split('@', 1)[0].casefold() if words else ''
    if command in {'/start', '/help'}:
        _send_help(chat_id)
        return
    if command == '/reports':
        _send_reports(chat_id, user_id)
        return

    youtube_url = _youtube_url_from_text(text, message.get('entities'))
    if not youtube_url:
        _send_message(
            chat_id,
            'Send a valid YouTube URL. Use /help for instructions.',
        )
        return

    video_id = youtube_video_id(youtube_url)
    existing_report = _get_store().find_report_for_video(video_id)
    if existing_report is not None:
        _send_existing_report(chat_id, user_id, video_id, existing_report)
        return

    try:
        job_id, created = _get_store().create_job(
            update_id,
            youtube_url,
            user_id,
            chat_id,
        )
    except QueueAdmissionError as error:
        if error.reason == 'user_already_processing':
            rejection_message = (
                'Your video has already started processing. Please wait for it to '
                'finish before sending another.'
            )
        elif error.reason == 'queue_full':
            rejection_message = (
                'The queue is full: one active job and up to three waiting users. '
                'Please try again when a slot opens.'
            )
        else:
            raise
        _send_message(chat_id, rejection_message)
        return
    if not created:
        _send_message(
            chat_id,
            'This request has already been received. Send a new message to start '
            'another comparison.',
        )
        return
    logger.info('Accepted video submission for job %s.', job_id)

    try:
        _send_message(
            chat_id,
            'Video received and added to the queue. The bot processes one video at a time.',
        )
    finally:
        _submit_job(_discover_job, job_id)


def _rerun_report(video_id, update_id, user_id, chat_id):
    if (
        isinstance(update_id, bool)
        or not isinstance(update_id, int)
        or not isinstance(video_id, str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id)
        or not _valid_telegram_ids(user_id, chat_id)
    ):
        return 400, 'This report action is invalid or has expired.'
    try:
        job_id, created = _get_store().create_job(
            update_id,
            f'https://www.youtube.com/watch?v={video_id}',
            user_id,
            chat_id,
        )
    except QueueAdmissionError as error:
        if error.reason == 'user_already_processing':
            return 409, 'Your video has already started processing. Please wait for it to finish.'
        if error.reason == 'queue_full':
            return 409, 'The queue is full. Please try again when a slot opens.'
        raise
    if not created:
        return 200, 'This rerun has already been received.'
    return 202, job_id


def _select_language(job_id, track_index, user_id, chat_id):
    if not _valid_telegram_ids(user_id, chat_id):
        return 400, 'Invalid Telegram identifiers.'
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        return 404, 'This language selection has expired.'
    job = _get_store().get_job(job_id)
    if job is None:
        return 404, 'This language selection has expired.'
    if job['user_id'] != user_id or job['chat_id'] != chat_id:
        return 403, 'This request belongs to another user.'
    if job['status'] != 'awaiting_language':
        return 409, 'This language selection is no longer available.'
    if (
        isinstance(track_index, bool)
        or not isinstance(track_index, int)
        or track_index < 0
        or track_index >= len(job['tracks'])
    ):
        return 400, 'Invalid language selection.'

    track = job['tracks'][track_index]
    logger.info(
        'Selected %s subtitles for job %s.',
        track['language_name'],
        job_id,
    )
    if not _get_store().update_job(
        job_id,
        expected_status='awaiting_language',
        status='queued',
        selected_track=track,
    ):
        return 409, 'This language selection is no longer available.'
    return 202, 'Language selected. Starting transcription.'


def _handle_callback_query(callback, update_id=None):
    callback_id = callback.get('id')
    callback_data = callback.get('data')
    message = callback.get('message')
    user = callback.get('from')
    language_match = (
        re.fullmatch(r'lang\|([a-f0-9]{32})\|(\d{1,2})', callback_data)
        if isinstance(callback_data, str)
        else None
    )
    report_match = (
        re.fullmatch(r'report\|([a-f0-9]{32})', callback_data)
        if isinstance(callback_data, str)
        else None
    )
    details_match = (
        re.fullmatch(r'details\|([a-f0-9]{32})', callback_data)
        if isinstance(callback_data, str)
        else None
    )
    rerun_match = (
        re.fullmatch(r'again\|([A-Za-z0-9_-]{11})', callback_data)
        if isinstance(callback_data, str)
        else None
    )
    access_match = (
        re.fullmatch(r'access\|([1-9]\d{0,19})', callback_data)
        if isinstance(callback_data, str)
        else None
    )
    if (
        not isinstance(callback_id, str)
        or not isinstance(message, dict)
        or not isinstance(user, dict)
        or not (
            language_match
            or report_match
            or details_match
            or rerun_match
            or access_match
        )
    ):
        if isinstance(callback_id, str):
            _answer_callback(callback_id, 'This selection is invalid or has expired.')
        return

    chat = message.get('chat')
    if not isinstance(chat, dict):
        _answer_callback(callback_id, 'This selection is invalid or has expired.')
        return
    chat_id = chat.get('id')
    user_id = user.get('id')
    if not _valid_telegram_ids(user_id, chat_id):
        _answer_callback(callback_id, 'This selection is invalid or has expired.')
        return

    if access_match:
        if user_id != _owner_chat_id():
            _answer_callback(callback_id, 'Only the owner can approve access.')
            return
        approved_user = _get_store().approve_user(int(access_match.group(1)))
        if approved_user is None:
            _answer_callback(callback_id, 'This access request is no longer available.')
            return
        _answer_callback(callback_id, 'Access approved.')
        _send_message(
            approved_user['chat_id'],
            'Your access has been approved. You can now use the bot.',
        )
        return

    if report_match or details_match:
        report_id = (report_match or details_match).group(1)
        report = _get_store().get_report(report_id)
        if report is None:
            _answer_callback(callback_id, 'This saved report is no longer available.')
            return
        if (
            user_id != _owner_chat_id()
            and not _get_store().user_can_access_report(report_id, user_id)
        ):
            _answer_callback(callback_id, 'This report belongs to another user.')
            return
        _answer_callback(callback_id, 'Sending saved report.')
        try:
            if details_match:
                _send_report_details(chat_id, report)
            else:
                _send_saved_report(chat_id, report)
        except (RuntimeError, ValueError):
            logger.exception('Could not send saved report %s.', report_id)
            _send_message(
                chat_id,
                'Could not send the saved report files. Please try again later.',
            )
        return

    if rerun_match:
        status, reply = _rerun_report(
            rerun_match.group(1),
            update_id,
            user_id,
            chat_id,
        )
        if status != 202:
            _answer_callback(callback_id, reply)
            if status == 409:
                _send_message(chat_id, reply)
            return
        job_id = reply
        try:
            _answer_callback(callback_id, 'Starting a new comparison.')
            _send_message(
                chat_id,
                'Video received and added to the queue. The bot processes one video at a time.',
            )
        finally:
            _submit_job(_discover_job, job_id)
        return

    job_id = language_match.group(1)
    status, reply = _select_language(
        job_id,
        int(language_match.group(2)),
        user_id,
        chat_id,
    )
    if status == 202:
        progress_callback = None
        try:
            _answer_callback(callback_id, reply)
            progress_callback = _start_progress_message(
                chat_id,
                message.get('message_id'),
                'Language selected. Preparing the audio and subtitle download...',
            )
        finally:
            _submit_job(_process_job, job_id, progress_callback)
    else:
        _answer_callback(callback_id, reply)


def _handle_update(update):
    update_id = update['update_id']
    message = update.get('message')
    callback = update.get('callback_query')
    if isinstance(message, dict):
        _handle_message(message, update_id)
    elif isinstance(callback, dict):
        _handle_callback_query(callback, update_id)


def _recover_jobs():
    for saved_job in _get_store().active_jobs():
        job_id = saved_job['job_id']
        status = saved_job['status']
        try:
            if status == 'discovering':
                _submit_job(_discover_job, job_id)
            elif status in {'queued', 'processing'}:
                if status == 'processing':
                    _get_store().update_job(job_id, status='queued')
                _submit_job(_process_job, job_id)
            elif status == 'awaiting_language':
                _send_language_prompt(job_id)
        except Exception:
            logger.exception('Could not recover job %s.', job_id)


def _poll_updates():
    offset = _get_store().get_offset()
    while True:
        updates = _telegram_call(
            'getUpdates',
            {
                'offset': offset,
                'timeout': 50,
                'allowed_updates': ['message', 'callback_query'],
            },
            timeout=60,
        )
        if not isinstance(updates, list):
            raise RuntimeError('Telegram returned an invalid getUpdates result.')
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get('update_id')
            if isinstance(update_id, bool) or not isinstance(update_id, int):
                logger.warning('Ignoring Telegram update without a valid update_id.')
                continue
            try:
                _handle_update(update)
            except Exception:
                logger.exception('Could not handle Telegram update %s.', update_id)
            offset = max(offset, update_id + 1)
            _get_store().set_offset(offset)


def main():
    global _store, _executor
    _required_environment('TELEGRAM_BOT_TOKEN')
    _owner_chat_id()
    database_path = os.environ.get('BOT_DATABASE_PATH', 'data/bot.sqlite3')
    _store = JobStore(database_path)
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='video-job')
    try:
        _telegram_call('deleteWebhook', {'drop_pending_updates': False})
        logger.info('Webhook removed; starting Telegram long polling.')
        _recover_jobs()
        while True:
            try:
                _poll_updates()
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception('Telegram polling failed; retrying in 5 seconds.')
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info('Stopping the bot.')
    finally:
        _executor.shutdown(wait=True)
        _store.close()
        _executor = None
        _store = None


if __name__ == '__main__':
    main()
