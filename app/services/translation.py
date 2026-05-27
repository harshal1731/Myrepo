from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
import logging

MODEL_NAME = "facebook/nllb-200-distilled-600M"
logging.info("Loading NLLB AI Model into memory...")
try:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME)
except:
    pass

def translate_dutch_to_english(dutch_text: str) -> str:
    if not dutch_text or str(dutch_text).strip() == "": return ""
    try:
        inputs = tokenizer(dutch_text, return_tensors="pt")
        translated_tokens = model.generate(**inputs, forced_bos_token_id=tokenizer.lang_code_to_id["eng_Latn"], max_length=200)
        return tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)[0]
    except Exception as e:
        logging.error(f"Translation Error: {e}")
        return dutch_text
