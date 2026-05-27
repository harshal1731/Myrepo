import logging
import re
import threading
from contextlib import nullcontext

from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

from app.config import NLLB_MODEL_NAME

try:
    import torch
except ImportError:  # pragma: no cover - torch is expected in production requirements.
    torch = None

_MODEL_LOCK = threading.Lock()
_tokenizer = None
_model = None
_load_failed = False

_DUTCH_INVOICE_MARKERS = (
    "aantal",
    "bedrag",
    "beveiliging",
    "btw",
    "conform",
    "factuur",
    "gecertificeerd",
    "gerecycled",
    "handdoek",
    "handzeep",
    "onderhoud",
    "omschrijving",
    "prijsopgaaf",
    "schoonmaak",
    "stuk",
    "uur",
    "vellen",
    "vloeibaar",
    "zwemband",
)
_GLOSSARY_REPLACEMENTS = (
    (r"\buur beveiliging\b", "security hours"),
    (r"\bbeveiliging\b", "security"),
    (r"\bconform\b", "according to"),
    (r"\bprijsopgaaf\b", "quote"),
    (r"\bbtw\b", "VAT"),
    (r"\bonderhoud\b", "maintenance"),
    (r"\bschoonmaak\b", "cleaning"),
    (r"\bgecertificeerd\b", "certified"),
    (r"\bgerecycled\b", "recycled"),
    (r"\bhanddoek(?:en)?\b", "towel"),
    (r"\bwit\b", "white"),
    (r"\blaags\b", "layer"),
    (r"\bstuks\b", "pieces"),
    (r"\bvellen\b", "sheets"),
    (r"\bhygi(?:e|\u00eb)nezakjes\b", "hygiene bags"),
    (r"\bhandzeep\b", "hand soap"),
    (r"\bvloeibaar\b", "liquid"),
    (r"\blavendel\b", "lavender"),
    (r"\bopblaasbare\b", "inflatable"),
    (r"\bzwemband\b", "swim ring"),
    (r"\bjanuari\b", "January"),
    (r"\bfebruari\b", "February"),
    (r"\bmaart\b", "March"),
    (r"\bmei\b", "May"),
    (r"\bjuni\b", "June"),
    (r"\bjuli\b", "July"),
    (r"\baugustus\b", "August"),
    (r"\bseptember\b", "September"),
    (r"\boktober\b", "October"),
    (r"\bnovember\b", "November"),
    (r"\bdecember\b", "December"),
)


def _looks_like_dutch_invoice_text(text: str) -> bool:
    lowered = text.lower()
    if any(re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in _DUTCH_INVOICE_MARKERS):
        return True
    return bool(re.search(r"[\u00c0-\u017f]", text))


def _glossary_translate(text: str) -> str:
    translated = text
    for pattern, replacement in _GLOSSARY_REPLACEMENTS:
        translated = re.sub(pattern, replacement, translated, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", translated).strip()


def _informative_tokens(text: str) -> set[str]:
    stop_words = {
        "and",
        "for",
        "het",
        "the",
        "van",
        "voor",
        "with",
    }
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]{3,}", text or "")
        if token.lower() not in stop_words
    }


def _looks_hallucinated(source: str, translated: str) -> bool:
    if not translated.strip():
        return True

    source_numbers = set(re.findall(r"\d+(?:[.,/-]\d+)*", source or ""))
    translated_numbers = set(re.findall(r"\d+(?:[.,/-]\d+)*", translated or ""))
    if source_numbers and not source_numbers.issubset(translated_numbers):
        return True

    source_tokens = _informative_tokens(source)
    translated_tokens = _informative_tokens(translated)
    if not source_tokens or not translated_tokens:
        return False

    overlap = source_tokens & translated_tokens
    translated_is_much_longer = len(translated_tokens) > max(len(source_tokens) * 3, 8)
    no_anchor_tokens_survived = not overlap and len(source_tokens) >= 3 and len(translated_tokens) >= 5
    return translated_is_much_longer or no_anchor_tokens_survived


def _load_model() -> bool:
    global _tokenizer, _model, _load_failed

    if _tokenizer is not None and _model is not None:
        return True
    if _load_failed:
        return False

    with _MODEL_LOCK:
        if _tokenizer is not None and _model is not None:
            return True
        try:
            logging.info("Loading NLLB translation model: %s", NLLB_MODEL_NAME)
            _tokenizer = AutoTokenizer.from_pretrained(NLLB_MODEL_NAME)
            _model = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL_NAME)
            if hasattr(_tokenizer, "src_lang"):
                _tokenizer.src_lang = "nld_Latn"
            return True
        except Exception as exc:
            _load_failed = True
            logging.error("Unable to load NLLB model: %s", exc)
            return False


def _english_token_id() -> int:
    lang_map = getattr(_tokenizer, "lang_code_to_id", None)
    if isinstance(lang_map, dict) and "eng_Latn" in lang_map:
        return lang_map["eng_Latn"]
    return _tokenizer.convert_tokens_to_ids("eng_Latn")


def translate_dutch_to_english(dutch_text: str) -> str:
    text = str(dutch_text or "").strip()
    if not text:
        return ""
    if not _looks_like_dutch_invoice_text(text):
        return text
    if not _load_model():
        return _glossary_translate(text)

    try:
        if hasattr(_tokenizer, "src_lang"):
            _tokenizer.src_lang = "nld_Latn"
        inputs = _tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        )
        context = torch.no_grad() if torch is not None else nullcontext()
        with context:
            translated_tokens = _model.generate(
                **inputs,
                forced_bos_token_id=_english_token_id(),
                max_length=256,
            )
        translated = _tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)[0]
        if _looks_hallucinated(text, translated):
            fallback = _glossary_translate(text)
            logging.warning("Rejected hallucinated invoice translation. source=%r translated=%r", text, translated)
            return fallback
        return translated
    except Exception as exc:
        logging.error("Translation Error: %s", exc)
        return _glossary_translate(text)
