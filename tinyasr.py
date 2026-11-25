import logging
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


TINYASR_URL = 'https://dkaraush.github.io/tinyasr/web/'
logger = logging.getLogger(__name__)


class TinyASRError(RuntimeError):
    pass


def transcribe(audio_path, model_label, language_code, progress_callback=None):
    audio_path = Path(audio_path).resolve()
    profile_dir = Path(
        os.environ.get(
            'TINYASR_PROFILE_DIR',
            str(Path(__file__).resolve().parent / 'data' / 'tinyasr-profile'),
        )
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    logger.info('Launching TinyASR Chromium session.')
    if progress_callback is not None:
        progress_callback('Step 2/5: Opening TinyASR and loading its model...')
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
        )
        try:
            page = context.new_page()
            logger.info('Opening TinyASR web application.')
            page.goto(TINYASR_URL, wait_until='domcontentloaded', timeout=120_000)

            logger.info('Waiting for TinyASR model initialization.')
            ready_deadline = time.monotonic() + 600
            ready_started_at = time.monotonic()
            next_ready_progress = ready_started_at + 60
            while time.monotonic() < ready_deadline:
                status = page.locator('#status').inner_text().strip()
                if status.startswith(('Failed to start:', 'Error:')):
                    raise TinyASRError(f'TinyASR could not start: {status}')
                if status.startswith('Ready') and not page.locator('#file').is_disabled():
                    break
                now = time.monotonic()
                if progress_callback is not None and now >= next_ready_progress:
                    elapsed_minutes = int((now - ready_started_at) // 60)
                    progress_callback(
                        'Step 2/5: TinyASR is still initializing '
                        f'({elapsed_minutes} min elapsed)...'
                    )
                    next_ready_progress = now + 60
                page.wait_for_timeout(1_000)
            else:
                raise TinyASRError('TinyASR did not become ready within 10 minutes.')

            logger.info(
                'TinyASR is ready; selecting model %s and language %s.',
                model_label,
                language_code,
            )
            if progress_callback is not None:
                progress_callback('Step 3/5: Transcribing audio with TinyASR...')
            page.locator('#model').select_option(label=model_label)
            page.locator('#lang').select_option(language_code)
            logger.info('Submitting audio to TinyASR.')
            page.locator('#file').set_input_files(str(audio_path))

            deadline = time.monotonic() + 1_800
            transcription_started_at = time.monotonic()
            next_transcription_progress = transcription_started_at + 60
            while time.monotonic() < deadline:
                status = page.locator('#status').inner_text().strip()
                if status.startswith('Done ('):
                    transcript = page.locator('#out').inner_text().strip()
                    if not transcript:
                        raise TinyASRError('TinyASR returned an empty transcript.')
                    logger.info('TinyASR transcription completed.')
                    return transcript
                if status.startswith(
                    ('Error:', 'Failed to start:', 'Could not decode')
                ):
                    raise TinyASRError(f'TinyASR transcription failed: {status}')

                download_status = page.locator('#dl')
                if download_status.is_visible():
                    message = download_status.inner_text().strip()
                    if message.startswith('Could not download'):
                        raise TinyASRError(f'TinyASR model download failed: {message}')
                now = time.monotonic()
                if progress_callback is not None and now >= next_transcription_progress:
                    elapsed_minutes = int((now - transcription_started_at) // 60)
                    progress_callback(
                        'Step 3/5: TinyASR is still transcribing '
                        f'({elapsed_minutes} min elapsed)...'
                    )
                    next_transcription_progress = now + 60
                page.wait_for_timeout(1_000)

            raise TinyASRError('TinyASR did not finish within 30 minutes.')
        finally:
            context.close()
