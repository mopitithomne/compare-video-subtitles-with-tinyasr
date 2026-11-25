LANGUAGE_MODELS = {
    'en': ('English', 'English, Russian, Ukrainian'),
    'ru': ('Russian', 'English, Russian, Ukrainian'),
    'uk': ('Ukrainian', 'English, Russian, Ukrainian'),
    'es': ('Spanish', 'English, Spanish, Italian, Portuguese'),
    'it': ('Italian', 'English, Spanish, Italian, Portuguese'),
    'pt': ('Portuguese', 'English, Spanish, Italian, Portuguese'),
    'uz': ('Uzbek', 'Uzbek'),
    'fa': ('Persian', 'English, Persian'),
    'km': ('Khmer', 'English, Khmer'),
    'ar': ('Arabic', 'Arabic (standard + 11 dialects)'),
    'id': ('Indonesian', 'Indonesian, Malay'),
    'ms': ('Malay', 'Indonesian, Malay'),
    'fr': ('French', 'French, German'),
    'de': ('German', 'French, German'),
}

MODEL_LANGUAGE_CODES = {
    'uz': 'uz-Latn',
}


def base_language(language_code):
    return language_code.replace('_', '-').split('-', 1)[0].casefold()


def supported_tracks(info):
    tracks_by_language = {}
    sources = (
        ('manual', info.get('subtitles') or {}),
        ('automatic', info.get('automatic_captions') or {}),
    )
    for source, language_tracks in sources:
        for language_code in sorted(language_tracks):
            language = base_language(language_code)
            model = LANGUAGE_MODELS.get(language)
            if model is None or language in tracks_by_language:
                continue
            tracks_by_language[language] = {
                'language_code': language,
                'model_language_code': MODEL_LANGUAGE_CODES.get(language, language),
                'subtitle_code': language_code,
                'language_name': model[0],
                'model_label': model[1],
                'source': source,
            }
    return sorted(
        tracks_by_language.values(),
        key=lambda track: track['language_name'].casefold(),
    )
