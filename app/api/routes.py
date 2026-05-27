from typing import Optional

from fastapi import APIRouter, File, UploadFile, HTTPException
import logging
from app.models.schemas import YardiResponse, MasterDataPayload
from app.services.ocr_service import AzureOcrError, extract_invoice_data_from_memory
from app.services.translation import translate_dutch_to_english
from app.services.mapper_service import generate_yardi_payload

from app.services.excel_service import (
    load_master_data_from_json,
    load_expense_report_from_memory,
)

router = APIRouter()


def _payload_dict(payload: MasterDataPayload) -> dict:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload.dict()


def _validate_extension(file: UploadFile, allowed: tuple[str, ...], label: str) -> None:
    filename = (file.filename or "").lower()
    if not filename.endswith(allowed):
        raise HTTPException(status_code=400, detail=f"{label} must be {', '.join(allowed)}")


# --- NEW ENDPOINT 1: JSON Master Data ---
@router.post("/api/update-master-data")
async def update_master_data(payload: MasterDataPayload):
    """Receives JSON from .NET containing Vendor and Property Master Data."""
    try:
        result = load_master_data_from_json(_payload_dict(payload))
        return {
            "message": "Master data (Vendors & Properties) loaded into RAM successfully.",
            "loaded": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"JSON RAM Load Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process JSON into RAM: {str(e)}")

# --- ENDPOINT 2: Excel Expense Report ---
@router.post("/api/upload-expense-report")
async def upload_expense_report(
    expense_file: Optional[UploadFile] = File(default=None),
):
    """Receives optional Expense Distribution Excel file and stores it in Pandas RAM."""
    if expense_file is None:
        return {
            "message": "No expense_file supplied. Existing in-memory expense data was unchanged.",
            "loaded": {},
        }

    try:
        loaded = {}

        _validate_extension(expense_file, (".xls", ".xlsx"), "Expense file")
        expense_bytes = await expense_file.read()
        loaded["expense_file"] = load_expense_report_from_memory(expense_bytes)

        return {
            "message": "Uploaded Excel data loaded into RAM successfully.",
            "loaded": loaded,
        }
    except HTTPException:
        raise
    except ValueError as e:
        logging.error(f"Excel validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error(f"Excel RAM Upload Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process Excel into RAM: {str(e)}")

@router.post("/api/process-invoice", response_model=YardiResponse)
async def process_invoice(file: UploadFile = File(...)):
    """Orchestrates OCR, Translation, and Formatting for Vendor Invoice"""
    if not (file.filename or "").lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Must be PDF")
        
    try:
        pdf_bytes = await file.read()
        ocr_data = extract_invoice_data_from_memory(pdf_bytes)
        
        logging.info("Starting Translation...")
        english_desc = translate_dutch_to_english(ocr_data.get("Description_Dutch", ""))
        ocr_data["Description_English"] = english_desc
        for item in ocr_data.get("InvoiceItems", []) or []:
            if not isinstance(item, dict):
                continue
            item_desc = item.get("Description_Dutch") or item.get("Description") or ""
            item["Description_English"] = (
                translate_dutch_to_english(item_desc) if item_desc else english_desc
            )
        logging.info(f"Translated Description: {english_desc}")
        
        final_payload = generate_yardi_payload(ocr_data, file.filename)
        logging.info(f"Final Status: {final_payload['status']}")
        
        return final_payload
    except HTTPException:
        raise
    except AzureOcrError as e:
        logging.error(f"Azure OCR error on {file.filename}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logging.error(f"Error on {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
