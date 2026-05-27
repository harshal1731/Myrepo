from fastapi import APIRouter, File, UploadFile, HTTPException
import logging
from app.models.schemas import YardiResponse, MasterDataPayload
from app.services.ocr_service import extract_invoice_data_from_memory
from app.services.translation import translate_dutch_to_english
from app.services.mapper_service import generate_yardi_payload

from app.services.excel_service import load_master_data_from_json, load_expense_report_from_memory

router = APIRouter()

# --- NEW ENDPOINT 1: JSON Master Data ---
@router.post("/api/update-master-data")
async def update_master_data(payload: MasterDataPayload):
    """Receives JSON from .NET containing Vendor and Property Master Data."""
    try:
        # Pass the dictionary directly to the Pandas service
        load_master_data_from_json(payload.dict())
        return {"message": "Master data (Vendors & Properties) loaded into RAM successfully."}
    except Exception as e:
        logging.error(f"JSON RAM Load Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process JSON into RAM: {str(e)}")

# --- ENDPOINT 2: Excel Expense Report ---
@router.post("/api/upload-expense-report")
async def upload_expense_report(expense_file: UploadFile = File(...)):
    """Receives Excel file from .NET and stores it directly in Pandas RAM."""
    try:
        if not expense_file.filename.endswith(('.xls', '.xlsx')):
            raise HTTPException(status_code=400, detail="Expense File must be Excel (.xlsx)")
        
        expense_bytes = await expense_file.read()
        load_expense_report_from_memory(expense_bytes)
        
        return {"message": "Expense Report loaded into RAM successfully."}
    except Exception as e:
        logging.error(f"Excel RAM Upload Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process Excel into RAM: {str(e)}")

# --- ENDPOINT 3: Process Invoice (Unchanged) ---
# Inside your process_invoice endpoint, update it to look like this:

@router.post("/api/process-invoice", response_model=YardiResponse)
async def process_invoice(file: UploadFile = File(...)):
    """Orchestrates OCR, Translation, and Formatting for Vendor Invoice"""
    if not file.filename.lower().endswith('.pdf'): 
        raise HTTPException(status_code=400, detail="Must be PDF")
        
    try:
        pdf_bytes = await file.read()
        ocr_data = extract_invoice_data_from_memory(pdf_bytes)
        
        logging.info("Starting Translation...")
        english_desc = translate_dutch_to_english(ocr_data.get("Description_Dutch", ""))
        ocr_data["Description_English"] = english_desc
        logging.info(f"Translated Description: {english_desc}")
        
        final_payload = generate_yardi_payload(ocr_data, file.filename)
        logging.info(f"Final Status: {final_payload['status']}")
        
        return final_payload
    except Exception as e:
        logging.error(f"Error on {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=str(e))