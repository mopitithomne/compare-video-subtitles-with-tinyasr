import json
import logging
import math
import os
import subprocess
from pathlib import Path

from yt_dlp import YoutubeDL

from languages import base_language, supported_tracks
from transcript import (
    compare_texts,
    read_reference,
    read_subtitle_cues,
    write_difference_files,
)
from tinyasr import transcribe


MAX_VIDEO_SECONDS = 3_600
MAX_AUDIO_BYTES = 500 * 1024 * 1024
MAX_TELEGRAM_UPLOAD_BYTES = 45_000_000
logger = logging.getLogger(__name__)


class AudioLanguageError(ValueError):
    pass


def _configure_yt_dlp_options(options):
    cookie_file = os.environ.get('YTDLP_COOKIES_FILE')
    if cookie_file:
        options['cookiefile'] = cookie_file
    return options


def _extract_info(url, download=False):
    options = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'js_runtimes': {'deno': {}},
    }
    logger.info('Requesting YouTube metadata (download=%s).', download)
    with YoutubeDL(_configure_yt_dlp_options(options)) as ydl:
        info = ydl.extract_info(url, download=download)
    if not isinstance(info, dict) or info.get('_type') == 'playlist':
        raise ValueError('The URL must point to a single YouTube video.')
    logger.info('YouTube metadata received.')
    return info


def inspect_video(url):
    info = _extract_info(url)
    duration = info.get('duration')
    if duration is not None and duration > MAX_VIDEO_SECONDS:
        raise ValueError('The video is longer than the 60-minute limit.')
    audio_languages = _audio_language_codes(info)
    if not audio_languages:
        raise AudioLanguageError(
            "Could not identify the video's audio language, so matching subtitles "
            'cannot be selected.'
        )
    tracks = [
        track
        for track in supported_tracks(info)
        if track['language_code'] in audio_languages
    ]
    return {
        'title': info.get('title') or 'YouTube video',
        'duration': duration,
        'tracks': tracks,
    }


def _bitrate(video_format):
    try:
        return float(video_format.get('abr') or video_format.get('tbr') or 0)
    except (TypeError, ValueError):
        return 0


def _audio_language_codes(info):
    languages = {
        base_language(video_format['language'])
        for video_format in info.get('formats') or []
        if isinstance(video_format, dict)
        and video_format.get('acodec') not in (None, 'none')
        and video_format.get('vcodec') in (None, 'none')
        and isinstance(video_format.get('language'), str)
        and video_format['language'].casefold() not in {'', 'und', 'unknown'}
    }
    if languages:
        return languages
    video_language = info.get('language')
    if isinstance(video_language, str) and video_language.casefold() not in {
        '',
        'und',
        'unknown',
    }:
        return {base_language(video_language)}
    return set()


def select_audio_format(info, language_code):
    audio_formats = [
        video_format
        for video_format in info.get('formats') or []
        if isinstance(video_format, dict)
        and video_format.get('acodec') not in (None, 'none')
        and video_format.get('vcodec') in (None, 'none')
    ]
    matching = [
        video_format
        for video_format in audio_formats
        if isinstance(video_format.get('language'), str)
        and base_language(video_format['language']) == language_code
    ]
    if matching:
        return str(max(matching, key=_bitrate)['format_id'])

    tagged_languages = {
        video_format['language']
        for video_format in audio_formats
        if isinstance(video_format.get('language'), str)
        and video_format['language'].casefold() not in {'', 'und', 'unknown'}
    }
    if tagged_languages:
        raise AudioLanguageError(
            f'No YouTube audio track is tagged for {language_code}; available: '
            f'{", ".join(sorted(tagged_languages))}.'
        )
    video_language = info.get('language')
    if isinstance(video_language, str) and base_language(video_language) == language_code:
        return 'bestaudio/best'
    raise AudioLanguageError(
        'Could not verify an audio track in the same language as the subtitles.'
    )


def _download_inputs(url, track, output_dir):
    info = _extract_info(url)
    audio_format = select_audio_format(info, track['language_code'])
    video_id = str(info['id'])
    logger.info(
        'Downloading audio and %s subtitles.',
        track['source'],
    )
    options = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'format': audio_format,
        'outtmpl': str(output_dir / '%(id)s.%(ext)s'),
        'subtitleslangs': [track['subtitle_code']],
        'subtitlesformat': 'vtt/best',
        'writesubtitles': track['source'] == 'manual',
        'writeautomaticsub': track['source'] == 'automatic',
        'js_runtimes': {'deno': {}},
    }
    with YoutubeDL(_configure_yt_dlp_options(options)) as ydl:
        result = ydl.extract_info(url, download=True)
    logger.info('yt-dlp finished downloading audio and subtitles.')
    if not isinstance(result, dict):
        raise RuntimeError('yt-dlp did not return video metadata.')

    audio_files = [
        path
        for path in output_dir.glob(f'{video_id}.*')
        if path.suffix.casefold() not in {'.vtt', '.part', '.json'}
        and path.is_file()
    ]
    subtitle_files = sorted(output_dir.glob(f'{video_id}*.vtt'))
    if not audio_files:
        raise RuntimeError('yt-dlp did not create an audio file.')
    if len(subtitle_files) != 1:
        raise RuntimeError(
            f'Expected one downloaded subtitle file, found {len(subtitle_files)}.'
        )
    audio_path = audio_files[0]
    audio_size = audio_path.stat().st_size
    if audio_size > MAX_AUDIO_BYTES:
        raise ValueError('The downloaded audio is larger than the 500 MB limit.')
    logger.info(
        'Downloaded audio is %d bytes; found one subtitle file.',
        audio_size,
    )
    return audio_path, subtitle_files[0], result.get('title') or info.get('title')


def _telegram_audio_file(audio_path, output_dir):
    if (
        audio_path.suffix.casefold() == '.mp3'
        and audio_path.stat().st_size <= MAX_TELEGRAM_UPLOAD_BYTES
    ):
        return audio_path, False

    compressed_path = output_dir / 'audio-for-telegram.mp3'
    logger.info('Converting audio to MP3 for Telegram playback.')
    try:
        subprocess.run(
            [
                'ffmpeg',
                '-nostdin',
                '-y',
                '-v',
                'error',
                '-i',
                str(audio_path),
                '-vn',
                '-ac',
                '1',
                '-ar',
                '22050',
                '-b:a',
                '64k',
                '-f',
                'mp3',
                str(compressed_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError('FFmpeg is required to prepare audio for Telegram.') from None
    except subprocess.CalledProcessError:
        raise RuntimeError('Could not compress audio for Telegram delivery.') from None

    if compressed_path.stat().st_size > MAX_TELEGRAM_UPLOAD_BYTES:
        raise ValueError("The audio still exceeds Telegram's upload limit after compression.")
    return compressed_path, True


def process_video(
    url,
    track,
    output_dir,
    progress_callback=None,
    transcript_callback=None,
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path, subtitle_path, title = _download_inputs(url, track, output_dir)
    logger.info('Reading downloaded subtitle reference.')
    reference = read_reference(subtitle_path)
    subtitle_cues = read_subtitle_cues(subtitle_path)
    if progress_callback is not None:
        progress_callback('Step 2/5: Preparing TinyASR and loading its model...')
    logger.info(
        'Starting TinyASR transcription with model %s for language %s.',
        track['model_label'],
        track['model_language_code'],
    )
    hypothesis = transcribe(
        audio_path,
        track['model_label'],
        track['model_language_code'],
        progress_callback,
        transcript_callback=transcript_callback,
    )
    logger.info('TinyASR transcription completed.')
    transcript_path = output_dir / 'tinyasr-transcript.txt'
    transcript_path.write_text(hypothesis + '\n', encoding='utf-8')
    logger.info('Comparing transcript with subtitle reference.')
    if progress_callback is not None:
        progress_callback('Step 4/5: Comparing the transcription with subtitles...')
    scores = compare_texts(reference, hypothesis, track['language_code'])
    differences, html_path, json_path = write_difference_files(
        output_dir,
        title or 'YouTube video',
        subtitle_cues,
        hypothesis,
        track['language_code'],
    )
    logger.info(
        'Text comparison complete: WER=%.2f%%, CER=%.2f%%, differing subtitle cues=%d/%d.',
        scores['wer'],
        scores['cer'],
        len(differences),
        len(subtitle_cues),
    )
    if progress_callback is not None:
        progress_callback('Step 5/5: Preparing result files for Telegram...')
    delivery_audio_path, audio_converted = _telegram_audio_file(audio_path, output_dir)
    return {
        'title': title or 'YouTube video',
        'audio_path': delivery_audio_path,
        'subtitle_path': subtitle_path,
        'transcript_path': transcript_path,
        'differences_html_path': html_path,
        'alignment_path': output_dir / 'subtitle-alignment.json',
        'differences_json_path': json_path,
        'differences': differences,
        'difference_count': len(differences),
        'subtitle_cue_count': len(subtitle_cues),
        'audio_converted': audio_converted,
        **scores,
    }


def _transcribe_uploaded_media(
    media_path,
    output_dir,
    media_type,
    progress_callback=None,
    transcript_callback=None,
):
    media_label = 'audio file' if media_type == 'audio' else 'video'
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if progress_callback is not None:
        progress_callback(
            'Preparing uploaded audio for TinyASR...'
            if media_type == 'audio'
            else 'Extracting audio from the uploaded video...'
        )
    try:
        probe = subprocess.run(
            [
                'ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration:stream=codec_type',
                '-of',
                'json',
                str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError('FFprobe is required to inspect uploaded media.') from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError(f'The uploaded {media_label} is not readable.') from None
    try:
        probe_data = json.loads(probe.stdout)
        duration = float(probe_data['format']['duration'])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(f'Could not determine the uploaded {media_label} duration.') from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f'Could not determine the uploaded {media_label} duration.')
    if duration > MAX_VIDEO_SECONDS:
        raise ValueError('Uploaded media is limited to 60 minutes.')
    streams = probe_data.get('streams')
    if not isinstance(streams, list) or not any(
        stream.get('codec_type') == 'audio'
        for stream in streams
        if isinstance(stream, dict)
    ):
        raise ValueError(f'The uploaded {media_label} does not contain an audio track.')

    audio_filename = (
        'uploaded-video-audio.mp3'
        if media_type == 'video'
        else 'uploaded-audio.mp3'
    )
    audio_path = output_dir / audio_filename
    try:
        subprocess.run(
            [
                'ffmpeg',
                '-nostdin',
                '-y',
                '-v',
                'error',
                '-i',
                str(media_path),
                '-map',
                '0:a:0',
                '-vn',
                '-ac',
                '1',
                '-ar',
                '16000',
                '-b:a',
                '64k',
                '-f',
                'mp3',
                str(audio_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        raise RuntimeError('FFmpeg is required to prepare audio for transcription.') from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError('Could not prepare audio from the uploaded media.') from None

    if progress_callback is not None:
        progress_callback(
            f'Transcribing the uploaded {media_label} with TinyASR...'
        )
    transcript = transcribe(
        audio_path,
        None,
        None,
        progress_callback,
        auto_detect=True,
        transcript_callback=transcript_callback,
    )
    transcript_path = output_dir / 'tinyasr-transcript.txt'
    transcript_path.write_text(transcript + '\n', encoding='utf-8')
    return transcript_path


def transcribe_uploaded_video(
    video_path,
    output_dir,
    progress_callback=None,
    transcript_callback=None,
):
    return _transcribe_uploaded_media(
        video_path,
        output_dir,
        'video',
        progress_callback,
        transcript_callback,
    )


def transcribe_uploaded_audio(
    audio_path,
    output_dir,
    progress_callback=None,
    transcript_callback=None,
):
    return _transcribe_uploaded_media(
        audio_path,
        output_dir,
        'audio',
        progress_callback,
        transcript_callback,
    )
