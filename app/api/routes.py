from typing import Optional

from fastapi import APIRouter, File, Form, Header, UploadFile, HTTPException
from fastapi.responses import JSONResponse
import logging
from app.models.schemas import InvoiceStatusResponse, MasterDataPayload, YardiResponse
from app.services.ocr_service import AzureOcrError, extract_invoice_data_from_memory
from app.services.translation import translate_dutch_to_english
from app.services.mapper_service import generate_yardi_payload, get_invoice_status_response

from app.services.excel_service import (
    load_master_data_from_json,
    load_expense_report_from_memory,
)

router = APIRouter()


def _payload_dict(payload) -> dict:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload.dict()


def _error_response(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": False, "message": message, "loaded": {}},
    )


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
            "status": True,
            "message": "Master data (Vendors & Properties) loaded into RAM successfully.",
            "loaded": {
                "vendorsCount": result.get("vendors", 0),
                "propertiesCount": result.get("properties", 0),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"JSON RAM Load Error: {e}")
        return _error_response(f"Failed to process JSON into RAM: {str(e)}", status_code=400)

# --- ENDPOINT 2: Excel Expense Report ---
@router.post("/api/upload-expense-report")
async def upload_expense_report(
    expense_file: Optional[UploadFile] = File(default=None),
):
    """Receives optional Expense Distribution Excel file and stores it in Pandas RAM."""
    if expense_file is None:
        return {
            "status": False,
            "message": "No expense_file supplied. Existing in-memory expense data was unchanged.",
            "loaded": {},
        }

    try:
        loaded = {}

        _validate_extension(expense_file, (".xls", ".xlsx"), "Expense file")
        expense_bytes = await expense_file.read()
        expense_result = load_expense_report_from_memory(expense_bytes)
        loaded["expense_file"] = {
            "rows_processed": expense_result.get("rows", 0),
            "status": True,
        }

        return {
            "status": True,
            "message": "Uploaded Excel data loaded into RAM successfully.",
            "loaded": loaded,
        }
    except HTTPException as e:
        return _error_response(str(e.detail), status_code=e.status_code)
    except ValueError as e:
        logging.error(f"Excel validation error: {e}")
        return _error_response(str(e), status_code=400)
    except Exception as e:
        logging.error(f"Excel RAM Upload Error: {e}")
        return _error_response(f"Failed to process Excel into RAM: {str(e)}", status_code=500)

@router.post("/api/process-invoice", response_model=YardiResponse)
async def process_invoice(
    file: UploadFile = File(...),
    azure_ocr_key: Optional[str] = Form(default=None, alias="azure-ocr-key"),
    azure_ocr_key_header: Optional[str] = Header(default=None, alias="azure-ocr-key"),
):
    """Orchestrates OCR, Translation, and Formatting for Vendor Invoice"""
    if not (file.filename or "").lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Must be PDF")
        
    try:
        pdf_bytes = await file.read()
        request_azure_key = (azure_ocr_key or "").strip() or (
            azure_ocr_key_header or ""
        ).strip()
        # print("request_azure_key is ", request_azure_key)
        ocr_data = extract_invoice_data_from_memory(
            pdf_bytes,
            azure_key=request_azure_key,
        )
        
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
        logging.info(f"Final Status: {final_payload['InvoiceStatus']}")
        
        return final_payload
    except HTTPException:
        raise
    except AzureOcrError as e:
        logging.error(f"Azure OCR error on {file.filename}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logging.error(f"Error on {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/update-invoice", response_model=InvoiceStatusResponse)
async def update_invoice(payload: YardiResponse):
    """
    Validates a user-edited invoice payload from the UI.

    The .NET/UI layer should send the same JSON shape returned by
    /api/process-invoice. This endpoint does not re-run OCR; it only checks
    whether the edited ETL data now satisfies the posting rules.
    """
    try:
        payload_dict = _payload_dict(payload)
        etl_data = payload_dict.get("etl_data", {})
        status_info = get_invoice_status_response(etl_data)
        logging.info(
            "Updated invoice validation file=%r status=%s message=%r",
            payload_dict.get("vendor_file_name"),
            status_info["InvoiceStatus"],
            status_info["message"],
        )
        return status_info
    except Exception as e:
        logging.error(f"Update invoice validation error: {e}")
        return {
            "InvoiceStatus": "Review",
            "status": False,
            "message": f"Failed to validate updated invoice: {str(e)}",
        }
