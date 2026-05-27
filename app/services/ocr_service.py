import base64
import logging
import re
import time
from typing import Any

import requests

from app import config


class AzureOcrError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def extract_invoice_data_from_memory(pdf_bytes: bytes) -> dict:
    if not pdf_bytes:
        raise AzureOcrError("PDF upload is empty.")
    if not config.AZURE_KEY:
        raise AzureOcrError("AZURE_KEY environment variable is not configured.")

    model_names = [
        name
        for name in (config.AZURE_MODEL_NAME, config.AZURE_FALLBACK_MODEL_NAME)
        if name
    ]
    model_names = list(dict.fromkeys(model_names))
    errors: list[str] = []

    for index, model_name in enumerate(model_names):
        try:
            result = _analyze_with_model(pdf_bytes, model_name)
            parsed = parse_azure_response(result)
            if (
                model_name != config.AZURE_FALLBACK_MODEL_NAME
                and not _has_minimum_invoice_fields(parsed)
            ):
                raise AzureOcrError(
                    "Azure custom model result is missing critical invoice fields.",
                    retryable=True,
                )
            parsed["Azure_Model"] = model_name
            return parsed
        except AzureOcrError as exc:
            errors.append(f"{model_name}: {exc}")
            is_last_model = index == len(model_names) - 1
            if is_last_model or not exc.retryable:
                raise AzureOcrError("; ".join(errors), retryable=exc.retryable) from exc
            logging.warning("Azure model %s failed; falling back. Error: %s", model_name, exc)

    raise AzureOcrError("Azure OCR did not run.")


def _has_minimum_invoice_fields(parsed: dict) -> bool:
    critical_values = [
        parsed.get("Invoice_Number"),
        parsed.get("Vendor_Name"),
        parsed.get("Invoice_Date"),
        parsed.get("Amount"),
    ]
    populated = sum(
        1
        for value in critical_values
        if value not in (None, "", "NA", 0, 0.0)
    )
    has_line_amount = any(
        item.get("Amount") not in (None, "", "NA", 0, 0.0)
        for item in parsed.get("InvoiceItems", []) or []
        if isinstance(item, dict)
    )
    return populated >= 3 and has_line_amount


def _analyze_with_model(pdf_bytes: bytes, model_name: str) -> dict:
    payload = {"base64Source": base64.b64encode(pdf_bytes).decode("utf-8")}
    headers = {
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": config.AZURE_KEY,
    }
    params = {
        "api-version": config.AZURE_API_VERSION,
        "stringIndexType": "textElements",
    }
    post_url = (
        f"{config.AZURE_ENDPOINT}documentintelligence/documentModels/"
        f"{model_name}:analyze"
    )

    logging.info("Calling Azure model: %s", model_name)
    try:
        response = requests.post(
            post_url,
            headers=headers,
            params=params,
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise AzureOcrError(f"Azure OCR request failed: {exc}") from exc

    if response.status_code != 202:
        retryable = response.status_code not in {401, 403}
        raise AzureOcrError(
            f"Azure API Error {response.status_code}: {response.text}",
            retryable=retryable,
        )

    operation_url = response.headers.get("Operation-Location")
    request_id = response.headers.get("apim-request-id")
    if operation_url:
        get_url = operation_url
        get_params = None
    elif request_id:
        get_url = (
            f"{config.AZURE_ENDPOINT}documentintelligence/documentModels/"
            f"{model_name}/analyzeResults/{request_id}"
        )
        get_params = {"api-version": config.AZURE_API_VERSION}
    else:
        raise AzureOcrError(
            "Azure response did not include an operation location.",
            retryable=True,
        )

    deadline = time.monotonic() + config.AZURE_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(config.AZURE_POLL_INTERVAL_SECONDS)
        try:
            poll_response = requests.get(
                get_url,
                params=get_params,
                headers=headers,
                timeout=30,
            )
            poll_response.raise_for_status()
            result = poll_response.json()
        except requests.RequestException as exc:
            raise AzureOcrError(f"Azure OCR polling failed: {exc}") from exc
        except ValueError as exc:
            raise AzureOcrError("Azure OCR polling returned invalid JSON.") from exc

        status = str(result.get("status", "")).lower()
        logging.info("Azure OCR status for %s: %s", model_name, status or "unknown")
        if status == "succeeded":
            return result
        if status == "failed":
            raise AzureOcrError(
                f"Azure OCR processing failed: {result.get('error', result)}",
                retryable=True,
            )

    raise AzureOcrError("Azure OCR polling timed out.", retryable=True)


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _field_value(field: Any) -> Any:
    if not isinstance(field, dict):
        return field

    currency = field.get("valueCurrency")
    if isinstance(currency, dict):
        amount = currency.get("amount")
        if amount is not None:
            return amount

    address = field.get("valueAddress")
    if isinstance(address, dict):
        parts = [
            address.get("streetAddress"),
            address.get("postalCode"),
            address.get("city"),
            address.get("countryRegion"),
        ]
        address_text = ", ".join(str(part) for part in parts if part)
        if address_text:
            return address_text

    for key in (
        "valueString",
        "valueDate",
        "valueNumber",
        "valueInteger",
        "valuePhoneNumber",
        "content",
    ):
        value = field.get(key)
        if value not in (None, ""):
            return value

    return None


def _field_content(field: Any) -> str:
    if not isinstance(field, dict):
        return "" if field is None else str(field)
    return str(field.get("content") or field.get("valueString") or "").strip()


def _lookup_field(fields: dict[str, Any], aliases: list[str]) -> Any:
    lookup = {_normalise_key(key): value for key, value in fields.items()}
    for alias in aliases:
        field = lookup.get(_normalise_key(alias))
        if field is not None:
            return _field_value(field)
    return None


def _lookup_raw_field(fields: dict[str, Any], aliases: list[str]) -> Any:
    lookup = {_normalise_key(key): value for key, value in fields.items()}
    for alias in aliases:
        field = lookup.get(_normalise_key(alias))
        if field is not None:
            return field
    return None


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d,.\-]", "", text)
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
        return 0.0


def _numeric_or_na(value: Any) -> float | str:
    return "NA" if value in (None, "") else _safe_float(value)


def _currency_numeric_or_na(field: Any) -> float | str:
    if field in (None, ""):
        return "NA"
    if not isinstance(field, dict):
        return _numeric_or_na(field)

    content = _field_content(field)
    if content and ("(" in content or "-" in content):
        return _numeric_or_na(content)

    currency = field.get("valueCurrency")
    if isinstance(currency, dict) and currency.get("amount") is not None:
        return float(currency["amount"])

    return _numeric_or_na(_field_value(field))


def _tax_numeric_or_na(field: Any) -> float | str:
    if field in (None, ""):
        return "NA"
    if not isinstance(field, dict):
        return _numeric_or_na(field)

    content = _field_content(field)
    if re.search(r"\bzero\s+rated\b", content, re.IGNORECASE) or re.search(r"\b0\s*%", content):
        return 0.0
    if content and not re.search(r"\d", content):
        return "NA"

    return _currency_numeric_or_na(field)


def _field_pages(field: Any) -> set[int]:
    if not isinstance(field, dict):
        return set()
    pages = {
        int(region["pageNumber"])
        for region in field.get("boundingRegions", []) or []
        if isinstance(region, dict) and region.get("pageNumber") is not None
    }
    if pages:
        return pages
    if isinstance(field.get("valueObject"), dict):
        for child in field["valueObject"].values():
            pages.update(_field_pages(child))
    return pages


def _invoice_header_pages(fields: dict[str, Any]) -> set[int]:
    pages: set[int] = set()
    for alias in (
        "InvoiceId",
        "InvoiceNumber",
        "InvoiceDate",
        "DueDate",
        "InvoiceTotal",
        "AmountDue",
        "TotalTax",
    ):
        pages.update(_field_pages(_lookup_raw_field(fields, [alias])))
    return pages


def _invoice_identity_pages(fields: dict[str, Any]) -> set[int]:
    pages: set[int] = set()
    for alias in ("InvoiceId", "InvoiceNumber", "InvoiceDate", "DueDate"):
        pages.update(_field_pages(_lookup_raw_field(fields, [alias])))
    return pages


def _iban_from_text(text: str) -> str:
    labelled = re.search(r"\bIBAN\b\s*:?\s*([^\r\n]+)", text or "", re.IGNORECASE)
    if labelled:
        candidate = re.sub(r"[^A-Z0-9]", "", labelled.group(1).upper())
        if len(candidate) >= 15 and re.match(r"^[A-Z]{2}\d{2}", candidate):
            return candidate

    match = re.search(
        r"\b(?:NL|GB|DE|FR|BE|ES|IT|IE|LU)\d{2}(?:\s?[A-Z0-9]){11,30}\b",
        text or "",
        re.IGNORECASE,
    )
    return re.sub(r"\s+", "", match.group(0)).upper() if match else "NA"


def _currency_code(fields: dict[str, Any]) -> str:
    for alias in ("InvoiceTotal", "TotalAmount", "Amount", "Invoice Total"):
        raw = _lookup_raw_field(fields, [alias])
        currency = raw.get("valueCurrency") if isinstance(raw, dict) else None
        if isinstance(currency, dict):
            code = currency.get("currencyCode") or currency.get("currencySymbol")
            if code:
                return str(code)
    return str(_lookup_field(fields, ["Currency", "InvoiceCurrency", "TranCurrency"]) or "EUR")


def _extract_invoice_items(fields: dict[str, Any]) -> list[dict[str, Any]]:
    items_field = _lookup_raw_field(fields, ["Items", "InvoiceItems"])
    if not isinstance(items_field, dict):
        return []

    invoice_items: list[dict[str, Any]] = []
    raw_items = items_field.get("valueArray", []) or []
    identity_pages = _invoice_identity_pages(fields)
    header_pages = identity_pages or _invoice_header_pages(fields)
    item_pages = [_field_pages(item) for item in raw_items]
    if header_pages and any(pages & header_pages for pages in item_pages):
        filtered_items = [
            item for item, pages in zip(raw_items, item_pages) if pages & header_pages
        ]
        if len(filtered_items) != len(raw_items):
            logging.info(
                "Ignored %s OCR item(s) from attachment/non-header pages.",
                len(raw_items) - len(filtered_items),
            )
        raw_items = filtered_items

    for item in raw_items:
        item_obj = item.get("valueObject", {}) if isinstance(item, dict) else {}
        if not isinstance(item_obj, dict):
            continue

        description = _lookup_field(
            item_obj,
            ["Description", "description", "Omschrijving", "ItemDescription"],
        )
        amount_field = _lookup_raw_field(
            item_obj,
            ["Amount", "LineAmount", "TotalPrice", "NetAmount", "ItemTotal"],
        )
        tax_field = _lookup_raw_field(
            item_obj,
            ["Tax", "TaxAmount", "VAT", "VATAmount", "TotalTax", "btw"],
        )
        vat_rate = _lookup_field(
            item_obj,
            ["VATRate", "VatRate", "TaxRate", "Tax Rate", "btw percentage"],
        )
        invoice_items.append(
            {
                "Description_Dutch": str(description).replace("\n", " ") if description else "",
                "Amount": _currency_numeric_or_na(amount_field),
                "Tax_Amount": _tax_numeric_or_na(tax_field),
                "VAT_Rate": vat_rate,
            }
        )

    return invoice_items


def parse_azure_response(result: dict) -> dict:
    docs = result.get("analyzeResult", {}).get("documents", [])
    raw_text = result.get("analyzeResult", {}).get("content", "") or ""
    if not docs:
        logging.warning("Azure returned no document records.")
        return {
            "Invoice_Number": "NA",
            "Vendor_Name": "NA",
            "Property_Name": "NA",
            "Invoice_Date": None,
            "Due_Date": None,
            "Amount": "NA",
            "Tax_Amount": "NA",
            "VAT_Rate": None,
            "Description_Dutch": "",
            "IBAN": "NA",
            "Currency": "EUR",
            "InvoiceItems": [],
            "Raw_Text": raw_text,
        }

    fields = docs[0].get("fields", {}) or {}
    logging.info("Azure OCR fields extracted: %s", sorted(fields.keys()))

    invoice_items = _extract_invoice_items(fields)
    descriptions: list[str] = []
    general_desc = _lookup_field(fields, ["description", "Description", "InvoiceNotes", "Notes"])
    if general_desc:
        descriptions.append(str(general_desc).replace("\n", " "))
    descriptions.extend(
        item["Description_Dutch"]
        for item in invoice_items
        if item.get("Description_Dutch")
    )

    return {
        "Invoice_Number": _lookup_field(
            fields,
            ["InvoiceId", "InvoiceNumber", "Invoice Number", "invoice_number", "Reference"],
        ) or "NA",
        "Vendor_Name": _lookup_field(
            fields,
            ["VendorName", "Vendor Name", "issuer", "SupplierName", "Supplier"],
        ) or "NA",
        "Property_Name": _lookup_field(
            fields,
            [
                "CustomerName",
                "CustomerAddress",
                "PropertyName",
                "Property Name",
                "BillTo",
                "BillingAddress",
                "issue",
            ],
        ) or "NA",
        "Unit_Number": _lookup_field(
            fields,
            [
                "UnitNumber",
                "Unit Number",
                "ApartmentNumber",
                "Apartment Number",
                "RoomNumber",
                "Room Number",
            ],
        ) or "NA",
        "Invoice_Date": _lookup_field(fields, ["InvoiceDate", "Invoice Date", "date", "Date"]),
        "Due_Date": _lookup_field(
            fields,
            ["DueDate", "Due Date", "PaymentDueDate", "Payment Due Date", "InvoiceDueDate"],
        ),
        "Amount": _numeric_or_na(
            _lookup_field(fields, ["InvoiceTotal", "Invoice Total", "TotalAmount", "amount", "Amount"])
        ),
        "Tax_Amount": _numeric_or_na(
            _lookup_field(fields, ["TotalTax", "VAT", "VATAmount", "Tax", "btw", "vat"])
        ),
        "VAT_Rate": _lookup_field(fields, ["VATRate", "VatRate", "TaxRate", "Tax Rate", "btw percentage"]),
        "Description_Dutch": " | ".join(descriptions),
        "IBAN": _lookup_field(fields, ["IBAN", "BankAccount", "Bank Account"]) or _iban_from_text(raw_text),
        "Currency": _currency_code(fields),
        "InvoiceItems": invoice_items,
        "Raw_Text": raw_text,
    }
