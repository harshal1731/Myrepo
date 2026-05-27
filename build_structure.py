import os

folders = [
    "app", "app/api", "app/services", "app/models",
    "data/uploads", "logs"
]

files = {
    "requirements.txt": """fastapi
uvicorn
python-multipart
requests
pandas
openpyxl
thefuzz
transformers
torch
pydantic
""",
    
    "README.md": """# Greystar Python AI Microservice
To run the server locally:
1. `pip install -r requirements.txt`
2. Run `uvicorn app.main:app --reload`
3. Access API Docs at: `http://127.0.0.1:8000/docs`
Note: The .NET team must upload the Excel files via the `/api/upload-expense-report` endpoint before processing invoices.
""",
    
    "app/__init__.py": "", "app/api/__init__.py": "", 
    "app/services/__init__.py": "", "app/models/__init__.py": "",

    "app/config.py": """import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

for folder in [UPLOAD_DIR, LOGS_DIR]:
    os.makedirs(folder, exist_ok=True)

# Dynamic File Paths (Uploaded via API)
MASTER_COMBINED_PATH = os.path.join(UPLOAD_DIR, "Uploaded_Master_Combined.xlsx") 
EXPENSE_REPORT_PATH = os.path.join(UPLOAD_DIR, "Uploaded_Expense_Report.xlsx")

AZURE_ENDPOINT = ""
AZURE_KEY = ""
AZURE_MODEL_NAME = ""
""",

    "app/main.py": """import logging
import os
from fastapi import FastAPI
from app.api.routes import router
from app.config import LOGS_DIR
from app.services.excel_service import load_initial_data

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "api_errors.log")),
        logging.StreamHandler()
    ]
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)

app = FastAPI(title="Greystar Invoice AI API", version="1.0")
app.include_router(router)

# Attempt to load Excel files into memory on startup (if they already exist from a previous upload)
@app.on_event("startup")
async def startup_event():
    load_initial_data()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
""",

    "app/models/schemas.py": """from pydantic import BaseModel
from typing import Dict, Any

class YardiResponse(BaseModel):
    vendor_file_name: str
    status: str
    etl_data: Dict[str, Any]
""",

    "app/services/ocr_service.py": """import requests
import json
import time
import base64
import logging
from app import config

def extract_invoice_data_from_memory(pdf_bytes: bytes) -> dict:
    logging.info("Sending document to Azure Custom Model...")
    base64_encoded_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
    data = {"base64Source": base64_encoded_pdf}
    headers = {'Content-Type': 'application/json', 'Ocp-Apim-Subscription-Key': config.AZURE_KEY}
    params_post = {'api-version': '2024-11-30', 'stringIndexType': 'textElements'}
    post_url = f'{config.AZURE_ENDPOINT}documentintelligence/documentModels/{config.AZURE_MODEL_NAME}:analyze'
    
    response = requests.post(post_url, headers=headers, params=params_post, data=json.dumps(data))
    if response.status_code != 202:
        raise Exception(f"Azure API Error: {response.text}")

    request_id = response.headers.get('apim-request-id')
    time.sleep(4)
    get_url = f'{config.AZURE_ENDPOINT}documentintelligence/documentModels/{config.AZURE_MODEL_NAME}/analyzeResults/{request_id}'
    
    while True:
        resp_get = requests.get(get_url, params={'api-version': '2024-11-30'}, headers=headers)
        result = resp_get.json()
        if result.get("status") == 'succeeded':
            break
        elif result.get("status") == 'failed':
            raise Exception("Azure processing failed.")
        time.sleep(4)  

    return parse_azure_response(result)

def parse_azure_response(result: dict) -> dict:
    docs = result.get("analyzeResult", {}).get("documents", [])
    if not docs: return {}
    fields = docs[0].get("fields", {})
    
    def get_val(key):
        f = fields.get(key)
        return f.get("valueString") or f.get("valueDate") or f.get("valueNumber") or f.get("content") if f else None

    descriptions = [item.get("valueObject", {}).get("Description", {}).get("content") for item in fields.get("Items", {}).get("valueArray", []) if item.get("valueObject", {}).get("Description", {}).get("content")]
    
    return {
        "Invoice_Number": get_val("InvoiceId") or get_val("InvoiceNumber") or get_val("invoice_number"),
        "Vendor_Name": get_val("VendorName") or get_val("issuer"),
        "Property_Name": get_val("CustomerName") or get_val("issue"), 
        "Invoice_Date": get_val("InvoiceDate") or get_val("date"),
        "Amount": float(get_val("InvoiceTotal") or get_val("amount") or 0.0),
        "Tax_Amount": float(get_val("TotalTax") or get_val("vat") or 0.0),
        "Description_Dutch": " | ".join(descriptions) if descriptions else (get_val("description") or ""),
    }
""",

    "app/services/translation.py": """from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
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
""",

    "app/services/excel_service.py": """import pandas as pd
from thefuzz import process
import os
import logging
from app import config

df_master = pd.DataFrame()
df_vendor = pd.DataFrame()
df_expense = pd.DataFrame()

def reload_master_data():
    global df_master, df_vendor
    if os.path.exists(config.MASTER_COMBINED_PATH):
        try:
            # 1. Load Master Tracker
            df_master = pd.read_excel(config.MASTER_COMBINED_PATH, sheet_name='Master tracker (Entities)')
            df_master = df_master.dropna(how='all')
            valid_cols = [c for c in ['Asset/Project Name', "New 'brand' name", 'Abbreviation', 'Asset Name'] if c in df_master.columns]
            df_master[valid_cols] = df_master[valid_cols].ffill()
            
            # 2. Load Vendor List
            df_vendor = pd.read_excel(config.MASTER_COMBINED_PATH, sheet_name='Vendor name Phase -1')
            df_vendor = df_vendor.dropna(how='all')
            
            logging.info("Dynamic Master Data (Entities & Vendors) Loaded Successfully.")
        except Exception as e:
            logging.error(f"Failed to load Master Data: {e}")

def reload_expense_report():
    global df_expense
    if os.path.exists(config.EXPENSE_REPORT_PATH):
        try:
            df_expense = pd.read_excel(config.EXPENSE_REPORT_PATH, header=4).dropna(how='all')
            logging.info("Dynamic Expense Report Reloaded Successfully.")
        except Exception as e:
            logging.error(f"Failed to load Expense Report: {e}")

def load_initial_data():
    reload_master_data()
    reload_expense_report()

def get_vendor_person_code(raw_vendor_name: str) -> tuple:
    if df_vendor.empty or not raw_vendor_name: return "NA", raw_vendor_name
    vendors = df_vendor['Vendor name'].dropna().tolist()
    if not vendors: return "NA", raw_vendor_name
    
    best_match, score = process.extractOne(raw_vendor_name, vendors)
    if score >= 80:
        row = df_vendor[df_vendor['Vendor name'] == best_match].iloc[0]
        return str(row.get('PERSON', 'NA')), best_match
    return "NA", raw_vendor_name

def get_property_yardi_code(raw_property_name: str) -> str:
    if df_master.empty or not raw_property_name: return "NA"
    properties = df_master['Property name'].dropna().tolist()
    if not properties: return "NA"
    
    best_match, score = process.extractOne(raw_property_name, properties)
    if score >= 80:
        row = df_master[df_master['Property name'] == best_match].iloc[0]
        return str(row.get('Yardi code', 'NA'))
    return "NA"
""",

    "app/services/mapper_service.py": """from datetime import datetime, timedelta
from app.services.excel_service import get_property_yardi_code, get_vendor_person_code

def generate_yardi_payload(ocr_data: dict, original_filename: str) -> dict:
    inv_date_str = ocr_data.get("Invoice_Date")
    date_formatted = "NA"
    due_date_formatted = "NA"
    post_month = "NA"
    
    if inv_date_str:
        try:
            dt = datetime.fromisoformat(inv_date_str[:10]) 
            date_formatted = dt.strftime("%d/%m/%Y")
            post_month = dt.strftime("%m/%Y")
            vendor_name = str(ocr_data.get("Vendor_Name", ""))
            due_date_formatted = dt.strftime("%d/%m/%Y") if vendor_name.lower().startswith("greystar netherlands") else (dt + timedelta(days=30)).strftime("%d/%m/%Y")
        except: pass

    yardi_property_code = get_property_yardi_code(ocr_data.get("Property_Name", ""))
    person_code, matched_vendor = get_vendor_person_code(ocr_data.get("Vendor_Name", ""))

    yardi_fields = {
        "TRANNUM": 1, 
        "PROPERTY": yardi_property_code, 
        "PERSON": person_code, 
        "OFFSET": "11020000", 
        "AMOUNT": ocr_data.get("Amount", 0.0), 
        "DUEDATE": due_date_formatted,
        "DATE": date_formatted, 
        "POSTMONTH": post_month, 
        "ACCOUNT": "NA", 
        "ACCRUAL": "21010000",
        "NOTES": ocr_data.get("Description_English", "NA"), 
        "REF": ocr_data.get("Invoice_Number", "NA"),
        "SEGMENT1": "NA", "SEGMENT2": "NA", "SEGMENT3": "NA", "SEGMENT4": "NA", "SEGMENT5": "NA", 
        "SEGMENT6": "NA", "SEGMENT7": "NA", "SEGMENT8": "NA", "SEGMENT9": "NA", "SEGMENT10": "NA", 
        "SEGMENT11": "NA", "SEGMENT12": "NA", "EXCHANGERATE": "NA", "EXCHANGERATEDATE": "NA",
        "TAXAMOUNT1": "NA", "TAXAMOUNT2": "NA", "FROMDATE": date_formatted, "TODATE": date_formatted,
        "EXPENSETYPE": "Expense (Opex)", "DETAILNOTES": ocr_data.get("Description_English", "NA"),
        "DISPLAYTYPE": "NLVatApplicablePayable", "ISCONSOLIDATECHECKS": -1, "DETAILVATTRANTYPEID": "apresex",
        "DETAILTAXAMOUNT1": ocr_data.get("Tax_Amount", 0.0), 
        "DETAILVATRATEID": "Zero Rated" if ocr_data.get("Tax_Amount", 0.0) == 0 else "Uksr",
        "INTERNATIONALPAYMENTTYPE": "EFT"
    }

    status = "Ready to Post"
    for field in ["PROPERTY", "PERSON", "AMOUNT", "REF"]:
        if yardi_fields.get(field) in [None, "", "NA", "UNKNOWN", 0.0]:
            status = "In Review"
            break

    return {"vendor_file_name": original_filename, "status": status, "etl_data": yardi_fields}
""",

    "app/api/routes.py": """from fastapi import APIRouter, File, UploadFile, HTTPException
import shutil
import logging
from typing import Optional
from app import config
from app.models.schemas import YardiResponse
from app.services.ocr_service import extract_invoice_data_from_memory
from app.services.translation import translate_dutch_to_english
from app.services.mapper_service import generate_yardi_payload
from app.services.excel_service import reload_expense_report, reload_master_data

router = APIRouter()

@router.post("/api/upload-expense-report")
async def upload_excel_reports(
    master_file: Optional[UploadFile] = File(None),
    expense_file: Optional[UploadFile] = File(None)
):
    \"\"\"Receives Excel files from .NET and updates Pandas memory dynamically.\"\"\"
    messages = []
    
    try:
        # Handle Master File Upload
        if master_file:
            if not master_file.filename.endswith(('.xls', '.xlsx')):
                raise HTTPException(status_code=400, detail="Master File must be Excel (.xlsx)")
            with open(config.MASTER_COMBINED_PATH, "wb") as buffer:
                shutil.copyfileobj(master_file.file, buffer)
            reload_master_data()
            messages.append("Master Combined file updated and loaded successfully.")

        # Handle Expense File Upload
        if expense_file:
            if not expense_file.filename.endswith(('.xls', '.xlsx')):
                raise HTTPException(status_code=400, detail="Expense File must be Excel (.xlsx)")
            with open(config.EXPENSE_REPORT_PATH, "wb") as buffer:
                shutil.copyfileobj(expense_file.file, buffer)
            reload_expense_report()
            messages.append("Expense Report file updated and loaded successfully.")

        if not messages:
            return {"message": "No files were uploaded."}

        return {"message": " | ".join(messages)}
        
    except Exception as e:
        logging.error(f"Excel Upload Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process files: {str(e)}")

@router.post("/api/process-invoice", response_model=YardiResponse)
async def process_invoice(file: UploadFile = File(...)):
    \"\"\"Orchestrates OCR, Translation, and Formatting for Vendor Invoice\"\"\"
    if not file.filename.lower().endswith('.pdf'): raise HTTPException(status_code=400, detail="Must be PDF")
    try:
        pdf_bytes = await file.read()
        ocr_data = extract_invoice_data_from_memory(pdf_bytes)
        ocr_data["Description_English"] = translate_dutch_to_english(ocr_data.get("Description_Dutch", ""))
        return generate_yardi_payload(ocr_data, file.filename)
    except Exception as e:
        logging.error(f"Error on {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
"""
}

def create_structure():
    print("🚀 Building Dynamic Greystar Python API Structure...\n")
    for folder in folders:
        os.makedirs(folder, exist_ok=True)
        
    for file_path, content in files.items():
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        
    print("✅ All Done! Your microservice is ready.")
    print("Run using: uvicorn app.main:app --reload")
    print("Use /api/upload-expense-report via Swagger Docs (http://127.0.0.1:8000/docs) to upload your Excel files first!")

if __name__ == "__main__":
    create_structure()