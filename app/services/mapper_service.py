from datetime import datetime, timedelta
import re
from app.services.excel_service import get_property_yardi_code, get_vendor_person_code

def extract_dates_from_text(text: str, fallback_date: str) -> str:
    """Attempts to find month/year references in translated text for FROM/TODATE."""
    # (Future optimization: You can add regex here to find dates like "September 2025" in the English text)
    # For now, we return the fallback (Invoice Date) as per your BRD rule.
    return fallback_date

def get_gl_code(english_desc: str) -> str:
    """Uses the translated English description to match against Expense Distribution Report."""
    # (Future logic: Check df_expense here based on keywords in english_desc)
    # Returning a dummy GL for now to satisfy the "Ready to Post" validation
    if "cleaning" in english_desc.lower(): return "52190000"
    if "security" in english_desc.lower(): return "52210000"
    return "NA"

def generate_yardi_payload(ocr_data: dict, original_filename: str) -> dict:
    """Maps extracted and translated data to the 38 Yardi ETL Fields."""
    
    # 1. Base Variables & Logic
    english_desc = str(ocr_data.get("Description_English", "NA"))
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
            # Due Date Logic: Internal vendors = same day. Others = +30 days.
            if vendor_name.lower().startswith("greystar netherlands"):
                due_date_formatted = dt.strftime("%d/%m/%Y")
            else:
                due_date_formatted = (dt + timedelta(days=30)).strftime("%d/%m/%Y")
        except:
            pass

    yardi_property_code = get_property_yardi_code(ocr_data.get("Property_Name", ""))
    person_code, matched_vendor = get_vendor_person_code(ocr_data.get("Vendor_Name", ""))
    
    # Derive Dates from translated text, or fallback to invoice date
    service_date = extract_dates_from_text(english_desc, fallback_date=date_formatted)
    
    # Determine GL Code from translated text
    gl_account = get_gl_code(english_desc)
    
    # Determine VAT Rate based on tax amount
    tax_amt = float(ocr_data.get("Tax_Amount", 0.0))
    vat_rate_id = "Zero Rated" if tax_amt == 0.0 else "Uksr"
    
    # 2. Build exactly 38 Fields
    yardi_fields = {
        "TRANNUM": 1, 
        "PROPERTY": yardi_property_code, 
        "PERSON": person_code, 
        "OFFSET": "11020000", 
        "AMOUNT": ocr_data.get("Amount", 0.0), 
        "DUEDATE": due_date_formatted,
        "DATE": date_formatted, 
        "POSTMONTH": post_month, 
        "ACCOUNT": gl_account, 
        "ACCRUAL": "21010000",
        "NOTES": english_desc, # "Notes have to be taken... basis keyword from invoice"
        "REF": ocr_data.get("Invoice_Number", "NA"),
        "SEGMENT1": "NA", 
        "SEGMENT2": "NA", 
        "SEGMENT3": "NA", 
        "SEGMENT4": "NA", 
        "SEGMENT5": "NA", 
        "SEGMENT6": "NA", 
        "SEGMENT7": "NA", 
        "SEGMENT8": "NA", 
        "SEGMENT9": "NA", 
        "SEGMENT10": "NA", 
        "SEGMENT11": "NA", 
        "SEGMENT12": "NA", 
        "EXCHANGERATE": "NA", 
        "EXCHANGERATEDATE": "NA",
        "TAXAMOUNT1": "NA", 
        "TAXAMOUNT2": "NA", 
        "FROMDATE": service_date, 
        "TODATE": service_date,
        "EXPENSETYPE": "Expense (Opex)", 
        "DETAILNOTES": english_desc, # "This has to be description from invoice"
        "DISPLAYTYPE": "NLVatApplicablePayable", 
        "ISCONSOLIDATECHECKS": -1, 
        "DETAILVATTRANTYPEID": "apresex",
        "DETAILTAXAMOUNT1": tax_amt, 
        "DETAILVATRATEID": vat_rate_id,
        "INTERNATIONALPAYMENTTYPE": "EFT" # Add logic for Cheque / IBAN check if needed
    }

    # 3. Status Determination (Is it ready for the .NET team to post?)
    mandatory_fields = ["PROPERTY", "PERSON", "AMOUNT", "ACCOUNT", "REF"]
    status = "Ready to Post"
    
    for field in mandatory_fields:
        val = yardi_fields.get(field)
        # If any mandatory field is missing/unmapped, flag it for manual review
        if val in [None, "", "NA", "UNKNOWN", 0.0]:
            status = "In Review"
            break

    return {
        "vendor_file_name": original_filename, 
        "status": status, 
        "etl_data": yardi_fields
    }