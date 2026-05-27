import logging
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
    if not _load_model():
        return text

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
        return _tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)[0]
    except Exception as exc:
        logging.error("Translation Error: %s", exc)
        return text
