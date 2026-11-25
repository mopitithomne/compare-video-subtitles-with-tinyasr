import unittest

from validation import youtube_video_id


class YouTubeUrlTests(unittest.TestCase):
    def test_accepts_single_video_url_formats(self):
        video_id = 'dQw4w9WgXcQ'
        urls = (
            f'https://www.youtube.com/watch?v={video_id}',
            f'https://youtu.be/{video_id}',
            f'https://youtube.com/shorts/{video_id}',
        )

        self.assertEqual([youtube_video_id(url) for url in urls], [video_id] * 3)

    def test_rejects_non_video_and_unsafe_urls(self):
        urls = (
            'https://youtube.com/playlist?list=PL1234567890',
            'https://example.com/watch?v=dQw4w9WgXcQ',
            'https://user@youtube.com/watch?v=dQw4w9WgXcQ',
            'https://youtube.com:8080/watch?v=dQw4w9WgXcQ',
        )

        self.assertEqual([youtube_video_id(url) for url in urls], [None] * 4)


if __name__ == '__main__':
    unittest.main()
