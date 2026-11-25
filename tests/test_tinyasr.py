import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tinyasr import transcribe


class TinyASRTests(unittest.TestCase):
    def test_auto_detect_selects_automatic_mode_and_reads_completed_transcript(self):
        status = MagicMock()
        status.inner_text.side_effect = [
            'Ready',
            'Transcribing…',
            'Done: Français detected',
        ]
        file_input = MagicMock()
        file_input.is_disabled.return_value = False
        model = MagicMock()
        output = MagicMock()
        output.inner_text.side_effect = ['Bonjour', 'Bonjour.']
        download_status = MagicMock()
        download_status.is_visible.return_value = False
        page = MagicMock()
        locators = {
            '#status': status,
            '#file': file_input,
            '#model': model,
            '#out': output,
            '#dl': download_status,
        }
        selected_locators = []

        def locate(selector):
            selected_locators.append(selector)
            return locators[selector]

        page.locator.side_effect = locate
        context = MagicMock()
        context.new_page.return_value = page
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context
        manager = MagicMock()
        manager.__enter__.return_value = playwright

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict('os.environ', {'TINYASR_PROFILE_DIR': temp_dir}),
            patch('tinyasr.sync_playwright', return_value=manager),
        ):
            streamed = []
            transcript = transcribe(
                Path(temp_dir) / 'audio.mp3',
                None,
                None,
                auto_detect=True,
                transcript_callback=lambda text: streamed.append(text) or True,
            )

        self.assertEqual(transcript, 'Bonjour.')
        self.assertEqual(streamed, ['Bonjour', 'Bonjour.'])
        model.select_option.assert_called_once_with('auto')
        self.assertNotIn('#lang', selected_locators)
        file_input.set_input_files.assert_called_once()


if __name__ == '__main__':
    unittest.main()
