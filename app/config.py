import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

for folder in [UPLOAD_DIR, LOGS_DIR]:
    os.makedirs(folder, exist_ok=True)

# Dynamic File Paths (Uploaded via API)
MASTER_COMBINED_PATH = os.path.join(UPLOAD_DIR, "Uploaded_Master_Combined.xlsx") 
EXPENSE_REPORT_PATH = os.path.join(UPLOAD_DIR, "Uploaded_Expense_Report.xlsx")

AZURE_ENDPOINT = "https://greystarinvociedataextractionuk.cognitiveservices.azure.com/"
AZURE_KEY = "Bbatcj2ePCYZ6zdz5nnCIL3WsIfeZ9eyCQdOuAFolzvRo53R6X0dJQQJ99CAACmepeSXJ3w3AAALACOGhqPJ"
AZURE_MODEL_NAME = "Greystar_common_logic_UK_v.1.3"
# https://greystarinvociedataextractionuk.cognitiveservices.azure.com/documentintelligence/documentModels/Greystar_common_logic_UK_v.1.3:analyze