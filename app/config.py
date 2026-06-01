import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

# Keep the service stateless: uploaded workbooks/PDFs are never written to disk.
LOGS_DIR = os.getenv("LOGS_DIR", os.path.join(BASE_DIR, "logs"))
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "false").lower() in {"1", "true", "yes"}

if LOG_TO_FILE:
    os.makedirs(LOGS_DIR, exist_ok=True)

AZURE_ENDPOINT = os.getenv(
    "AZURE_ENDPOINT",
    "https://greystarinvociedataextractionuk.cognitiveservices.azure.com/",
).rstrip("/") + "/"
AZURE_KEY = "Bbatcj2ePCYZ6zdz5nnCIL3WsIfeZ9eyCQdOuAFolzvRo53R6X0dJQQJ99CAACmepeSXJ3w3AAALACOGhqPJ"
AZURE_MODEL_NAME = os.getenv("AZURE_MODEL_NAME", "Greystar_common_logic_UK_v.1.3")
AZURE_FALLBACK_MODEL_NAME = os.getenv("AZURE_FALLBACK_MODEL_NAME", "").strip()
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION", "2024-11-30")
AZURE_POLL_INTERVAL_SECONDS = float(os.getenv("AZURE_POLL_INTERVAL_SECONDS", "2"))
AZURE_POLL_TIMEOUT_SECONDS = int(os.getenv("AZURE_POLL_TIMEOUT_SECONDS", "120"))

NLLB_MODEL_NAME = os.getenv("NLLB_MODEL_NAME", "facebook/nllb-200-distilled-600M")
FUZZY_MATCH_THRESHOLD = int(os.getenv("FUZZY_MATCH_THRESHOLD", "80"))
EXPENSE_MATCH_THRESHOLD = int(os.getenv("EXPENSE_MATCH_THRESHOLD", "65"))
VAT_RATE_9_ID = os.getenv("VAT_RATE_9_ID", "NA")
