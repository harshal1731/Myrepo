import calendar
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from dateutil import parser as date_parser

from app.services.excel_service import (
    get_expense_account_match,
    get_property_match_details,
    get_vendor_person_code,
    get_vendor_person_code_from_text,
)
from app import config

YARDI_HEADER_FIELD_NAMES = [
    "PROPERTY",
    "PERSON",
    "OFFSET",
    "DUEDATE",
    "DATE",
    "POSTMONTH",
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
INVOICE_ITEM_FIELD_NAMES = ["TRANNUM", "ACCOUNT", "NOTES", "AMOUNT", "DETAILTAXAMOUNT1"]
YARDI_FIELD_NAMES = YARDI_HEADER_FIELD_NAMES

OFFSET_MAPPINGS = [
    {
        "entity": "GS Netherlands Bright C.V.",
        "yardiCode": "nlodrb-o",
        "newCashAccount": "11021-000",
    },
    {
        "entity": "GS Netherland AMC Student C.V.",
        "yardiCode": "nlamc2-o",
        "newCashAccount": "11021-000",
    },
    {
        "entity": "GS Netherland CDZ Parking C.V.",
        "yardiCode": "nlcdp2-o",
        "newCashAccount": "11020-000",
    },
    {
        "entity": "GS Netherland AMC C.V.",
        "yardiCode": "amcc2-o",
        "newCashAccount": "11021-000",
    },
    {
        "entity": "GS Netherlands CDZ C.V.",
        "yardiCode": "cdre2-o",
        "newCashAccount": "11020-000",
    },
    {
        "entity": "GS Netherlands CDZ - R Propco C.V.",
        "yardiCode": "cdrprp-o",
        "newCashAccount": "11020-000",
    },
    {
        "entity": "GS Netherlands CDZ Expansion C.V.",
        "yardiCode": "cdzx2-o",
        "newCashAccount": "11021-000",
    },
    {
        "entity": "Orange House ZC 2015 C.V.",
        "yardiCode": "gsoh-op",
        "newCashAccount": "11022-000",
    },
    {
        "entity": "GS Netherlands Onsite Manco B.V.",
        "yardiCode": "nlman-o",
        "newCashAccount": "11021-000",
    },
]

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


def _is_unavailable(value: Any) -> bool:
    if value is None:
        return True
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


def _default_invoice_date() -> datetime:
    return datetime(2025, 11, 1)


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


def _extract_vat_rate(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        rate = float(value)
        return rate * 100 if 0 < rate < 1 else rate

    text = str(value).strip()
    if text.upper() in _MISSING_TEXT:
        return None

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%?", text)
    if not match:
        return None

    try:
        rate = float(match.group(1).replace(",", "."))
        return rate * 100 if 0 < rate < 1 else rate
    except ValueError:
        return None


def _normalise_mapping_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _code_matches(property_code: str, mapping_code: str) -> bool:
    property_code = str(property_code or "").strip().lower()
    mapping_code = str(mapping_code or "").strip().lower()
    if not property_code or not mapping_code:
        return False
    return property_code == mapping_code or property_code == mapping_code.split("-", 1)[0]


def _offset_account(property_details: dict[str, Any]) -> str:
    yardi_code = _text_or_na(property_details.get("yardi_code")).lower()
    site_name = _text_or_na(property_details.get("site_name")).lower()
    site_key = _normalise_mapping_key(site_name)

    for mapping in OFFSET_MAPPINGS:
        if yardi_code and yardi_code != "na" and _code_matches(yardi_code, mapping["yardiCode"]):
            return mapping["newCashAccount"]
        if site_key and site_key != "na" and site_key == _normalise_mapping_key(mapping["entity"]):
            return mapping["newCashAccount"]

    for mapping in OFFSET_MAPPINGS:
        mapping_key = _normalise_mapping_key(mapping["entity"])
        if site_key and (mapping_key in site_key or site_key in mapping_key):
            return mapping["newCashAccount"]

    return "NA"


def _is_specific_property_hint(match_details: dict[str, Any]) -> bool:
    matched_text = _text_or_na(match_details.get("matched_text"))
    compact = re.sub(r"[^A-Za-z0-9]", "", matched_text)
    return 3 <= len(compact) <= 10 and compact.upper() == compact


def _item_text(item: dict[str, Any], fallback: str) -> str:
    return _text_or_na(
        item.get("Description_English")
        or item.get("Notes")
        or item.get("Description")
        or item.get("Description_Dutch")
        or item.get("description")
        or fallback
    )


def _item_amount(item: dict[str, Any]) -> float | str:
    for key in ("Amount", "LineAmount", "TotalPrice", "NetAmount", "amount"):
        if key in item and item.get(key) is not None:
            value = item.get(key)
            return "NA" if _text_or_na(value) == "NA" else safe_float(value)
    return "NA"


def _item_tax(item: dict[str, Any]) -> float | str:
    for key in ("Tax_Amount", "TaxAmount", "VAT", "VATAmount", "tax"):
        if key in item and item.get(key) is not None:
            value = item.get(key)
            return "NA" if _text_or_na(value) == "NA" else safe_float(value)
    return "NA"


def _computed_item_tax(amount: float | str, item: dict[str, Any]) -> float | str:
    if _is_unavailable(amount):
        return "NA"
    for key in ("VAT_Rate", "VatRate", "Tax_Rate", "TaxRate"):
        rate = _extract_vat_rate(item.get(key))
        if rate is not None:
            return round(safe_float(amount) * rate / 100, 2)
    return "NA"


def _raw_items(ocr_data: dict) -> list[dict[str, Any]]:
    raw_items = ocr_data.get("InvoiceItems") or ocr_data.get("Items") or []
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _build_invoice_items(ocr_data: dict, english_desc: str) -> list[dict[str, Any]]:
    raw_items = _raw_items(ocr_data)
    invoice_amount = "NA" if _text_or_na(ocr_data.get("Amount")) == "NA" else safe_float(ocr_data.get("Amount"))
    invoice_tax = "NA" if _text_or_na(ocr_data.get("Tax_Amount")) == "NA" else safe_float(ocr_data.get("Tax_Amount"))

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
        if single_item and _is_missing(amount):
            amount = invoice_amount
        if single_item and _is_unavailable(tax_amount):
            tax_amount = invoice_tax
        if _is_unavailable(tax_amount):
            tax_amount = _computed_item_tax(amount, item)

        invoice_items.append(
            {
                "TRANNUM": index,
                "ACCOUNT": "NA",
                "NOTES": _item_text(item, english_desc),
                "AMOUNT": amount,
                "DETAILTAXAMOUNT1": tax_amount,
            }
        )

    return invoice_items


def _line_tax_total(invoice_items: list[dict[str, Any]]) -> float:
    return sum(
        safe_float(item.get("DETAILTAXAMOUNT1"))
        for item in invoice_items
        if not _is_missing(item.get("DETAILTAXAMOUNT1"))
    )


def _status(etl_data: dict[str, Any]) -> str:
    return "Review" if _review_reasons(etl_data) else "Ready to Post"


def _review_reasons(etl_data: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if any(_is_missing(etl_data.get(field)) for field in ("PROPERTY", "PERSON", "OFFSET", "REF")):
        reasons.extend(
            field
            for field in ("PROPERTY", "PERSON", "OFFSET", "REF")
            if _is_missing(etl_data.get(field))
        )

    invoice_items = etl_data.get("InvoiceItems")
    if not isinstance(invoice_items, list) or not invoice_items:
        reasons.append("InvoiceItems")
        return reasons
    if any(not isinstance(item, dict) for item in invoice_items):
        reasons.append("InvoiceItems")
        return reasons

    for index, item in enumerate(invoice_items, start=1):
        if _is_missing(item.get("ACCOUNT")):
            reasons.append(f"InvoiceItems[{index}].ACCOUNT")
        if _is_missing(item.get("AMOUNT")):
            reasons.append(f"InvoiceItems[{index}].AMOUNT")
        if _is_unavailable(item.get("DETAILTAXAMOUNT1")):
            reasons.append(f"InvoiceItems[{index}].DETAILTAXAMOUNT1")

    return reasons


def generate_yardi_payload(ocr_data: dict, original_filename: str) -> dict:
    """Map extracted invoice data to the required nested Yardi response."""
    english_desc = _text_or_na(ocr_data.get("Description_English"))
    dutch_desc = _text_or_na(ocr_data.get("Description_Dutch"))
    raw_text = _text_or_na(ocr_data.get("Raw_Text"))
    raw_vendor_name = _text_or_na(ocr_data.get("Vendor_Name"))
    invoice_dt = parse_date(ocr_data.get("Invoice_Date"))
    date_was_defaulted = invoice_dt is None
    if date_was_defaulted:
        invoice_dt = _default_invoice_date()

    date_formatted = _format_date(invoice_dt)
    post_month = _text_or_na(ocr_data.get("Post_Month") or ocr_data.get("PostMonth"))
    if post_month == "NA":
        post_month = "12/2025" if date_was_defaulted else _format_post_month(invoice_dt)
    due_dt = parse_date(
        ocr_data.get("Due_Date")
        or ocr_data.get("DueDate")
        or ocr_data.get("Invoice_Due_Date")
        or ocr_data.get("PaymentDueDate")
    )
    if due_dt:
        due_date_formatted = _format_date(due_dt)
    elif invoice_dt and raw_vendor_name.lower().startswith("greystar netherlands"):
        due_date_formatted = _format_date(invoice_dt)
    elif invoice_dt:
        due_date_formatted = _format_date(invoice_dt + timedelta(days=30))
    else:
        due_date_formatted = "NA"

    invoice_items = _build_invoice_items(ocr_data, english_desc)
    property_query = (
        ocr_data.get("Property_Name")
        or ocr_data.get("Property_Address")
        or ocr_data.get("CustomerAddress")
        or ""
    )
    unit_number = (
        ocr_data.get("Unit_Number")
        or ocr_data.get("UnitNumber")
        or ocr_data.get("Apartment_Number")
        or ocr_data.get("ApartmentNumber")
        or ""
    )
    property_details = get_property_match_details(
        property_query,
        unit_number=unit_number,
        vendor_name=raw_vendor_name,
    )
    description_property_details = get_property_match_details(
        dutch_desc if dutch_desc != "NA" else english_desc,
        unit_number=unit_number,
        vendor_name=raw_vendor_name,
    )
    if (
        property_details.get("yardi_code") == "NA"
        or _is_specific_property_hint(description_property_details)
    ) and (
        description_property_details.get("yardi_code") != "NA"
        and int(description_property_details.get("score", 0)) >= 95
    ):
        property_details = description_property_details

    yardi_property_code = str(property_details.get("yardi_code", "NA"))
    offset_account = _offset_account(property_details)
    person_code, matched_vendor = get_vendor_person_code(raw_vendor_name)
    if person_code == "NA":
        fallback_person, fallback_vendor = get_vendor_person_code_from_text(raw_text)
        if fallback_person != "NA":
            person_code, matched_vendor = fallback_person, fallback_vendor

    account_match_text = " ".join(
        part for part in (english_desc, dutch_desc) if part and part != "NA"
    )
    header_match = get_expense_account_match(
        account_match_text,
        property_code=yardi_property_code,
        vendor_code=person_code,
        vendor_name=matched_vendor,
    )
    previous_posting_match: dict[str, Any] | None = None
    for item in invoice_items:
        item_notes = str(item.get("NOTES", ""))
        is_discount_line = (
            safe_float(item.get("AMOUNT")) < 0
            and re.search(r"\b(?:discount|korting|rebate)\b", item_notes, re.IGNORECASE)
        )
        if is_discount_line and previous_posting_match is not None:
            selected_match = previous_posting_match
            logging.info(
                "Discount line inherited account=%s notes=%r",
                selected_match.get("account"),
                selected_match.get("notes"),
            )
        else:
            item_match = get_expense_account_match(
                " ".join(
                    part
                    for part in (item_notes, english_desc, dutch_desc)
                    if part and part != "NA"
                ),
                property_code=yardi_property_code,
                vendor_code=person_code,
                vendor_name=matched_vendor,
            )
            selected_match = item_match if item_match.get("account") != "NA" else header_match
        item["ACCOUNT"] = str(selected_match.get("account", "NA"))
        if selected_match.get("notes") not in (None, "", "NA"):
            item["NOTES"] = str(selected_match["notes"])
        if not is_discount_line and item["ACCOUNT"] != "NA":
            previous_posting_match = selected_match

    from_date, to_date = extract_dates_from_text(english_desc, date_formatted)

    etl_data = {
        "PROPERTY": yardi_property_code,
        "PERSON": person_code,
        "OFFSET": offset_account,
        "DUEDATE": due_date_formatted,
        "DATE": date_formatted,
        "POSTMONTH": post_month,
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
        "DETAILVATRATEID": "Uksr",
        "INTERNATIONALPAYMENTTYPE": _payment_type(ocr_data),
        "InvoiceItems": invoice_items,
    }

    if list(etl_data.keys()) != YARDI_HEADER_FIELD_NAMES:
        raise RuntimeError("Yardi header field contract mismatch.")
    for item in invoice_items:
        if list(item.keys()) != INVOICE_ITEM_FIELD_NAMES:
            raise RuntimeError("Yardi line item field contract mismatch.")

    status = _status(etl_data)
    review_reasons = _review_reasons(etl_data)
    logging.info(
        "Yardi mapping summary file=%r status=%s property=%s person=%s offset=%s "
        "line_count=%s review_reasons=%s",
        original_filename,
        status,
        etl_data["PROPERTY"],
        etl_data["PERSON"],
        etl_data["OFFSET"],
        len(invoice_items),
        review_reasons,
    )

    return {
        "vendor_file_name": f"{original_filename} - {matched_vendor if person_code != 'NA' else raw_vendor_name}",
        "status": status,
        "etl_data": etl_data,
    }
