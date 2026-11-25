import unittest

from languages import supported_tracks


class SupportedTracksTests(unittest.TestCase):
    def test_keeps_only_tinyasr_languages_and_prefers_manual_tracks(self):
        info = {
            'subtitles': {
                'fr-CA': [{}],
                'ja': [{}],
            },
            'automatic_captions': {
                'fr': [{}],
                'de': [{}],
                'ja': [{}],
            },
        }

        tracks = supported_tracks(info)

        self.assertEqual(
            [(track['language_code'], track['source']) for track in tracks],
            [('fr', 'manual'), ('de', 'automatic')],
        )
        self.assertEqual(tracks[0]['model_language_code'], 'fr')

    def test_uzbek_uses_a_language_value_available_in_tinyasr(self):
        tracks = supported_tracks({'subtitles': {'uz-Cyrl': [{}]}})

        self.assertEqual(tracks[0]['language_code'], 'uz')
        self.assertEqual(tracks[0]['model_language_code'], 'uz-Latn')


if __name__ == '__main__':
    unittest.main()
