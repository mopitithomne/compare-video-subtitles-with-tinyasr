import re
from urllib.parse import parse_qs, urlsplit


VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def youtube_video_id(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None

    host = (parsed.hostname or '').casefold()
    if parsed.scheme not in {'http', 'https'}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in (None, 80 if parsed.scheme == 'http' else 443):
        return None
    if host == 'youtu.be':
        candidate = parsed.path.strip('/').split('/', 1)[0]
    elif host == 'youtube.com' or host.endswith('.youtube.com'):
        path_parts = parsed.path.strip('/').split('/')
        if parsed.path == '/watch':
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        elif len(path_parts) == 2 and path_parts[0] in {'shorts', 'live', 'embed'}:
            candidate = path_parts[1]
        else:
            return None
    else:
        return None

    return candidate if VIDEO_ID_RE.fullmatch(candidate) else None
