from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Mora:
    candidates: tuple[str, ...]
    duration_ms: int = 135
    pause_after_ms: int = 0


_ROMAJI_TO_KANA = {
    "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ", "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
    "sha": "しゃ", "shu": "しゅ", "sho": "しょ", "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ", "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ",
    "bya": "びゃ", "byu": "びゅ", "byo": "びょ", "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ",
    "mya": "みゃ", "myu": "みゅ", "myo": "みょ", "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ",
    "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ", "va": "ば", "vi": "び", "vu": "ぶ", "ve": "べ", "vo": "ぼ",
    "tsa": "つぁ", "tsi": "つぃ", "tse": "つぇ", "tso": "つぉ", "she": "しぇ", "che": "ちぇ", "je": "じぇ",
    "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ",
    "ga": "が", "gi": "ぎ", "gu": "ぐ", "ge": "げ", "go": "ご",
    "sa": "さ", "shi": "し", "si": "し", "su": "す", "se": "せ", "so": "そ",
    "za": "ざ", "ji": "じ", "zi": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ",
    "ta": "た", "chi": "ち", "ti": "ち", "tsu": "つ", "tu": "つ", "te": "て", "to": "と",
    "da": "だ", "di": "でぃ", "du": "どぅ", "de": "で", "do": "ど",
    "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね", "no": "の",
    "ha": "は", "hi": "ひ", "fu": "ふ", "hu": "ふ", "he": "へ", "ho": "ほ",
    "ba": "ば", "bi": "び", "bu": "ぶ", "be": "べ", "bo": "ぼ",
    "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ", "po": "ぽ",
    "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も",
    "ya": "や", "yu": "ゆ", "yo": "よ",
    "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
    "wa": "わ", "wi": "うぃ", "we": "うぇ", "wo": "を",
    "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お", "n": "ん",
}
_ROMAJI_KEYS = sorted(_ROMAJI_TO_KANA, key=len, reverse=True)
_PUNCT_PAUSES = {",": 150, ";": 190, ":": 170, ".": 310, "!": 300, "?": 330, "\n": 280}


def _katakana_to_hiragana(text: str) -> str:
    out: list[str] = []
    for char in text:
        code = ord(char)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(char)
    return "".join(out)


def _kana_candidates(kana: str) -> tuple[str, ...]:
    alternates: dict[str, tuple[str, ...]] = {
        "じ": ("じ", "ぢ", "ji"), "ず": ("ず", "づ", "zu"), "し": ("し", "shi", "si"),
        "ち": ("ち", "chi", "ti"), "つ": ("つ", "tsu", "tu"), "ふ": ("ふ", "fu", "hu"),
        "を": ("を", "お", "wo"), "ん": ("ん", "n"),
    }
    return alternates.get(kana, (kana,))


def _split_hiragana_mora(text: str) -> list[str]:
    small = set("ゃゅょぁぃぅぇぉゎ")
    result: list[str] = []
    for char in _katakana_to_hiragana(text):
        if char in small and result:
            result[-1] += char
        elif char == "ー" and result:
            result.append(result[-1][-1])
        elif "ぁ" <= char <= "ゖ" or char == "ん":
            result.append(char)
    return result


def _romaji_to_kana(text: str) -> list[str]:
    value = re.sub(r"[^a-z]", "", text.lower())
    result: list[str] = []
    index = 0
    while index < len(value):
        if index + 1 < len(value) and value[index] == value[index + 1] and value[index] not in "aeioun":
            index += 1
            continue
        matched = False
        for key in _ROMAJI_KEYS:
            if value.startswith(key, index):
                result.append(_ROMAJI_TO_KANA[key])
                index += len(key)
                matched = True
                break
        if not matched:
            char = value[index]
            fallback = {
                "b": "ぶ", "c": "く", "d": "ど", "f": "ふ", "g": "ぐ", "h": "ふ",
                "j": "じ", "k": "く", "l": "る", "m": "む", "p": "ぷ", "q": "く",
                "r": "る", "s": "す", "t": "と", "v": "ぶ", "w": "う", "x": "し", "y": "い", "z": "ず",
            }.get(char)
            if fallback:
                result.append(fallback)
            index += 1
    return result


def _portuguese_word_to_romaji(word: str) -> str:
    value = unicodedata.normalize("NFKD", word.lower()).replace("ç", "s")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    replacements = (
        (r"nh", "ny"), (r"lh", "ly"), (r"ch", "sh"), (r"rr", "r"), (r"ss", "s"),
        (r"qu(?=[ei])", "k"), (r"gu(?=[ei])", "g"), (r"ph", "f"),
        (r"c(?=[ei])", "s"), (r"g(?=[ei])", "j"), (r"c", "k"), (r"q", "k"),
        (r"x", "sh"), (r"w", "u"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    value = re.sub(r"[^a-z]", "", value)
    return value


def _word_to_mora(word: str) -> list[str]:
    if re.search(r"[ぁ-ゖァ-ヺ]", word):
        return _split_hiragana_mora(word)
    ascii_word = unicodedata.normalize("NFKC", word)
    if re.fullmatch(r"[A-Za-z]+", ascii_word):
        return _romaji_to_kana(_portuguese_word_to_romaji(ascii_word))
    return _romaji_to_kana(_portuguese_word_to_romaji(ascii_word))


def phonemize(text: str, *, max_moras: int = 240) -> list[Mora]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not normalized:
        return []
    tokens = re.findall(r"[ぁ-ゖァ-ヺーA-Za-zÀ-ÿÇç]+|[,.!?:;\n]", normalized)
    result: list[Mora] = []
    for token in tokens:
        if token in _PUNCT_PAUSES:
            if result:
                previous = result[-1]
                result[-1] = Mora(previous.candidates, previous.duration_ms, max(previous.pause_after_ms, _PUNCT_PAUSES[token]))
            continue
        moras = _word_to_mora(token)
        for mora in moras:
            duration = 150 if mora in {"ん"} else 135
            result.append(Mora(_kana_candidates(mora), duration_ms=duration))
            if len(result) >= max(1, int(max_moras)):
                return result
        if result:
            previous = result[-1]
            result[-1] = Mora(previous.candidates, previous.duration_ms, max(previous.pause_after_ms, 45))
    return result
