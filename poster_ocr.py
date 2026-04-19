"""Extract band/artist names from a festival poster image via Gemini vision.

Gemini handles stylized festival fonts (custom typography, vertical layouts,
all-caps headliners mixed with tiny undercard text) better than classical
OCR. In a single call it both reads the image and filters out non-band text
(sponsors, dates, stage names, taglines).
"""
import os
from urllib.parse import urlparse

import requests


GEMINI_MODEL = 'gemini-2.5-flash'

POSTER_PROMPT = """Extract every band or artist name from this festival poster.

Rules:
- Include headliners AND small-font / undercard names. Don't skip the tiny print.
- Ignore festival branding, sponsors, stage names, dates, venues, city names,
  taglines, hashtags, ticket info, and ampersands.
- If a name is stylized (weird spacing, custom font, all-caps), return the
  standard spelling that would match a music streaming search.
- Return one name per line. No numbering, no bullets, no commentary, no markdown.
"""


def _guess_mime(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.gif': 'image/gif',
    }.get(ext, 'image/jpeg')


def _load_image_bytes(image_source):
    """Accept a local file path or http(s) URL. Return (bytes, mime_type)."""
    parsed = urlparse(image_source)
    if parsed.scheme in ('http', 'https'):
        resp = requests.get(image_source, timeout=30)
        resp.raise_for_status()
        mime = resp.headers.get('Content-Type', '').split(';')[0].strip()
        return resp.content, mime or 'image/jpeg'

    with open(image_source, 'rb') as f:
        return f.read(), _guess_mime(image_source)


import re

_LEADING_JUNK_RE = re.compile(r'^[\s\-\u2022*]+|^\d+[.)]\s*')


def _parse_band_lines(text):
    """Parse Gemini's reply into a deduped, cleaned list of band names."""
    names = []
    seen = set()
    for raw in (text or '').splitlines():
        line = _LEADING_JUNK_RE.sub('', raw).strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(line)
    return names


def extract_bands_from_poster(image_source, api_key=None):
    """Call Gemini vision on the given image and return a list of band names.

    image_source: local file path or http(s) URL.
    Raises RuntimeError with a user-friendly message on setup issues.
    """
    api_key = api_key or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/app/apikey and run "
            "'set GEMINI_API_KEY=...' (Windows) or "
            "'export GEMINI_API_KEY=...' (macOS/Linux) before launching."
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "The 'google-genai' package is not installed. Run: "
            "pip install google-genai"
        ) from e

    image_bytes, mime_type = _load_image_bytes(image_source)

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            POSTER_PROMPT,
        ],
    )

    return _parse_band_lines(response.text)
