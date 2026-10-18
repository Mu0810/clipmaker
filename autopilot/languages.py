"""The languages Clip Maker handles end to end (design §15, after task 13.2;
Requirements 2.7, 4.4, 14.1): the ones Whisper transcribes, by
faster-whisper's codes, with their English names.

They fill the Presets screen's two language menus (`options`), decide which
spoken language a preset may set (`spoken`: Whisper refuses any other code,
so a source using one would fail at its transcript), and name the language
the AI metadata prompt asks for (`name`). tests/test_languages.py checks the
codes against the installed faster-whisper.
"""
from __future__ import annotations

NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani",
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bo": "Tibetan",
    "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh",
    "da": "Danish", "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fo": "Faroese",
    "fr": "French", "gl": "Galician", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian",
    "hy": "Armenian", "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "jw": "Javanese", "ka": "Georgian", "kk": "Kazakh", "km": "Khmer", "kn": "Kannada",
    "ko": "Korean", "la": "Latin", "lb": "Luxembourgish", "ln": "Lingala", "lo": "Lao",
    "lt": "Lithuanian", "lv": "Latvian", "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian",
    "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
    "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk", "no": "Norwegian",
    "oc": "Occitan", "pa": "Punjabi", "pl": "Polish", "ps": "Pashto", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala",
    "sk": "Slovak", "sl": "Slovenian", "sn": "Shona", "so": "Somali", "sq": "Albanian",
    "sr": "Serbian", "su": "Sundanese", "sv": "Swedish", "sw": "Swahili", "ta": "Tamil",
    "te": "Telugu", "tg": "Tajik", "th": "Thai", "tk": "Turkmen", "tl": "Tagalog",
    "tr": "Turkish", "tt": "Tatar", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek",
    "vi": "Vietnamese", "yi": "Yiddish", "yo": "Yoruba", "yue": "Cantonese", "zh": "Chinese",
}


def _base(code: str) -> str:
    return code.strip().split("-")[0].lower()


def name(code: str | None) -> str | None:
    """The English name of a code, or of a regional one's language ("pt-BR"
    is Portuguese); None when it isn't one of these."""
    if not code:
        return None
    return NAMES.get(code.strip()) or NAMES.get(_base(code))


def spoken(code: str) -> str | None:
    """The code Whisper transcribes for a spoken-language setting: the code
    itself, or a regional one's language ("en-US" is "en"); None when
    Whisper doesn't know the language."""
    code = code.strip()
    if code in NAMES:
        return code
    base = _base(code)
    return base if base in NAMES else None


def options() -> list[dict]:
    """[{code, name}] by name, for a menu."""
    return [{"code": c, "name": n} for c, n in sorted(NAMES.items(), key=lambda item: item[1])]
