import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

from dateutil import parser as date_parser

from app.services.excel_service import (
    get_expense_account_code,
    get_property_yardi_code,
    get_vendor_person_code,
)

YARDI_HEADER_FIELD_NAMES = [
    "PROPERTY",
    "PERSON",
    "OFFSET",
    "DUEDATE",
    "DATE",
    "POSTMONTH",
    "ACCOUNT",
    "ACCRUAL",
    "REF",
    "SEGMENT1",
    "SEGMENT2",
    "SEGMENT3",
    "SEGMENT4",
    "SEGMENT5",
    "SEGMENT6",
    "SEGMENT7",
    "SEGMENT8",
    "SEGMENT9",
    "SEGMENT10",
    "SEGMENT11",
    "SEGMENT12",
    "EXCHANGERATE",
    "EXCHANGERATEDATE",
    "TAXAMOUNT1",
    "TAXAMOUNT2",
    "FROMDATE",
    "TODATE",
    "EXPENSETYPE",
    "DETAILNOTES",
    "DISPLAYTYPE",
    "ISCONSOLIDATECHECKS",
    "DETAILVATTRANTYPEID",
    "DETAILVATRATEID",
    "INTERNATIONALPAYMENTTYPE",
    "InvoiceItems",
]
INVOICE_ITEM_FIELD_NAMES = ["TRANNUM", "NOTES", "AMOUNT", "DETAILTAXAMOUNT1"]
YARDI_FIELD_NAMES = YARDI_HEADER_FIELD_NAMES

_MISSING_TEXT = {"", "NA", "N/A", "NONE", "NULL", "UNKNOWN", "NAN"}
_MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE_PATTERN = re.compile(
    rf"\b(?:"
    rf"\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}|"
    rf"\d{{4}}[/-]\d{{1,2}}[/-]\d{{1,2}}|"
    rf"\d{{1,2}}\s+(?:{_MONTH_PATTERN})\s+\d{{4}}|"
    rf"(?:{_MONTH_PATTERN})\s+\d{{1,2}},?\s+\d{{4}}"
    rf")\b",
    re.IGNORECASE,
)
_MONTH_YEAR_PATTERN = re.compile(rf"\b({_MONTH_PATTERN})\s+(\d{{4}})\b", re.IGNORECASE)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return float(value) == 0.0
    return str(value).strip().upper() in _MISSING_TEXT


def _text_or_na(value: Any) -> str:
    if value is None:
        return "NA"
    text = re.sub(r"\s+", " ", str(value).strip())
    return "NA" if text.upper() in _MISSING_TEXT else text


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if text.upper() in _MISSING_TEXT:
        return default

    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return default

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[-1]
        text = text.replace(",", ".") if len(tail) == 2 else text.replace(",", "")

    try:
        number = float(text)
        return -abs(number) if negative else number
    except ValueError:
        return default


def parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, (int, float)) and 30000 <= float(value) <= 60000:
        return datetime(1899, 12, 30) + timedelta(days=float(value))

    text = str(value).strip()
    if text.upper() in _MISSING_TEXT:
        return None

    try:
        return datetime.fromisoformat(text[:10])
    except ValueError:
        pass

    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:
        return date_parser.parse(text, dayfirst=True, fuzzy=True)
    except (ValueError, TypeError, OverflowError):
        return None


def _format_date(dt: datetime | None) -> str:
    return dt.strftime("%d/%m/%Y") if dt else "NA"


def _format_post_month(dt: datetime | None) -> str:
    return dt.strftime("%m/%Y") if dt else "NA"


def extract_dates_from_text(text: str, fallback_date: str) -> tuple[str, str]:
    fallback = fallback_date if fallback_date != "NA" else "NA"
    text = text or ""

    parsed_dates = [parse_date(match.group(0)) for match in _DATE_PATTERN.finditer(text)]
    parsed_dates = [dt for dt in parsed_dates if dt is not None]
    if len(parsed_dates) >= 2:
        start, end = parsed_dates[0], parsed_dates[1]
        if start > end:
            start, end = end, start
        return _format_date(start), _format_date(end)
    if len(parsed_dates) == 1:
        formatted = _format_date(parsed_dates[0])
        return formatted, formatted

    month_match = _MONTH_YEAR_PATTERN.search(text)
    if month_match:
        month_dt = parse_date("1 " + month_match.group(0))
        if month_dt:
            last_day = calendar.monthrange(month_dt.year, month_dt.month)[1]
            return (
                _format_date(month_dt),
                _format_date(month_dt.replace(day=last_day)),
            )

    return fallback, fallback


def _payment_type(ocr_data: dict) -> str:
    iban = _text_or_na(ocr_data.get("IBAN")).replace(" ", "").upper()
    currency = _text_or_na(
        ocr_data.get("Currency")
        or ocr_data.get("Invoice_Currency")
        or ocr_data.get("TranCurrency")
        or "EUR"
    ).upper()

    if iban.startswith("GB"):
        return "Cheque"
    if currency not in {"EUR", "EURO", "\u20ac"}:
        return "Cheque"
    return "EFT"


def _item_text(item: dict[str, Any], fallback: str) -> str:
    return _text_or_na(
        item.get("Description_English")
        or item.get("Notes")
        or item.get("Description")
        or item.get("Description_Dutch")
        or item.get("description")
        or fallback
    )


def _item_amount(item: dict[str, Any]) -> float:
    return safe_float(
        item.get("Amount")
        or item.get("LineAmount")
        or item.get("TotalPrice")
        or item.get("NetAmount")
        or item.get("amount")
    )


def _item_tax(item: dict[str, Any]) -> float:
    return safe_float(
        item.get("Tax_Amount")
        or item.get("TaxAmount")
        or item.get("VAT")
        or item.get("VATAmount")
        or item.get("tax")
    )


def _raw_items(ocr_data: dict) -> list[dict[str, Any]]:
    raw_items = ocr_data.get("InvoiceItems") or ocr_data.get("Items") or []
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _build_invoice_items(ocr_data: dict, english_desc: str) -> list[dict[str, Any]]:
    raw_items = _raw_items(ocr_data)
    invoice_amount = safe_float(ocr_data.get("Amount"))
    invoice_tax = safe_float(ocr_data.get("Tax_Amount"))

    if not raw_items:
        raw_items = [
            {
                "Description_English": english_desc,
                "Amount": invoice_amount,
                "Tax_Amount": invoice_tax,
            }
        ]

    invoice_items: list[dict[str, Any]] = []
    single_item = len(raw_items) == 1
    for index, item in enumerate(raw_items, start=1):
        amount = _item_amount(item)
        tax_amount = _item_tax(item)
        if single_item and amount == 0.0:
            amount = invoice_amount
        if single_item and tax_amount == 0.0:
            tax_amount = invoice_tax

        invoice_items.append(
            {
                "TRANNUM": index,
                "NOTES": _item_text(item, english_desc),
                "AMOUNT": amount,
                "DETAILTAXAMOUNT1": tax_amount,
            }
        )

    return invoice_items


def _line_tax_total(invoice_items: list[dict[str, Any]]) -> float:
    return sum(safe_float(item.get("DETAILTAXAMOUNT1")) for item in invoice_items)


def _status(etl_data: dict[str, Any]) -> str:
    if any(_is_missing(etl_data.get(field)) for field in ("PROPERTY", "PERSON", "ACCOUNT", "REF")):
        return "In Review"

    invoice_items = etl_data.get("InvoiceItems")
    if not isinstance(invoice_items, list) or not invoice_items:
        return "In Review"
    if any(not isinstance(item, dict) for item in invoice_items):
        return "In Review"
    if any(_is_missing(item.get("AMOUNT")) for item in invoice_items):
        return "In Review"

    return "Ready to Post"


def generate_yardi_payload(ocr_data: dict, original_filename: str) -> dict:
    """Map extracted invoice data to the required nested Yardi response."""
    english_desc = _text_or_na(ocr_data.get("Description_English"))
    raw_vendor_name = _text_or_na(ocr_data.get("Vendor_Name"))
    invoice_dt = parse_date(ocr_data.get("Invoice_Date"))

    date_formatted = _format_date(invoice_dt)
    post_month = _format_post_month(invoice_dt)
    if invoice_dt and raw_vendor_name.lower().startswith("greystar netherlands"):
        due_date_formatted = _format_date(invoice_dt)
    elif invoice_dt:
        due_date_formatted = _format_date(invoice_dt + timedelta(days=30))
    else:
        due_date_formatted = "NA"

    invoice_items = _build_invoice_items(ocr_data, english_desc)
    tax_amt = safe_float(ocr_data.get("Tax_Amount"))
    if tax_amt == 0.0:
        tax_amt = _line_tax_total(invoice_items)

    property_query = (
        ocr_data.get("Property_Name")
        or ocr_data.get("Property_Address")
        or ocr_data.get("CustomerAddress")
        or ""
    )
    yardi_property_code = get_property_yardi_code(property_query)
    person_code, _ = get_vendor_person_code(raw_vendor_name)
    gl_account = get_expense_account_code(
        english_desc,
        property_code=yardi_property_code,
        vendor_code=person_code,
    )
    from_date, to_date = extract_dates_from_text(english_desc, date_formatted)

    etl_data = {
        "PROPERTY": yardi_property_code,
        "PERSON": person_code,
        "OFFSET": "11020000",
        "DUEDATE": due_date_formatted,
        "DATE": date_formatted,
        "POSTMONTH": post_month,
        "ACCOUNT": gl_account,
        "ACCRUAL": "21010000",
        "REF": _text_or_na(ocr_data.get("Invoice_Number")),
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
        "FROMDATE": from_date,
        "TODATE": to_date,
        "EXPENSETYPE": "Expense (Opex)",
        "DETAILNOTES": english_desc,
        "DISPLAYTYPE": "NLVatApplicablePayable",
        "ISCONSOLIDATECHECKS": -1,
        "DETAILVATTRANTYPEID": "apresex",
        "DETAILVATRATEID": "Zero Rated" if tax_amt == 0.0 else "Uksr",
        "INTERNATIONALPAYMENTTYPE": _payment_type(ocr_data),
        "InvoiceItems": invoice_items,
    }

    if list(etl_data.keys()) != YARDI_HEADER_FIELD_NAMES:
        raise RuntimeError("Yardi header field contract mismatch.")
    for item in invoice_items:
        if list(item.keys()) != INVOICE_ITEM_FIELD_NAMES:
            raise RuntimeError("Yardi line item field contract mismatch.")

    return {
        "vendor_file_name": original_filename,
        "status": _status(etl_data),
        "etl_data": etl_data,
    }
