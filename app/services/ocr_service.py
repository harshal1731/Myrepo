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
            parsed["Azure_Model"] = model_name
            return parsed
        except AzureOcrError as exc:
            errors.append(f"{model_name}: {exc}")
            is_last_model = index == len(model_names) - 1
            if is_last_model or not exc.retryable:
                raise AzureOcrError("; ".join(errors), retryable=exc.retryable) from exc
            logging.warning("Azure model %s failed; falling back. Error: %s", model_name, exc)

    raise AzureOcrError("Azure OCR did not run.")


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
    for item in items_field.get("valueArray", []) or []:
        item_obj = item.get("valueObject", {}) if isinstance(item, dict) else {}
        if not isinstance(item_obj, dict):
            continue

        description = _lookup_field(
            item_obj,
            ["Description", "description", "Omschrijving", "ItemDescription"],
        )
        amount = _lookup_field(
            item_obj,
            ["Amount", "LineAmount", "TotalPrice", "NetAmount", "ItemTotal"],
        )
        tax_amount = _lookup_field(
            item_obj,
            ["Tax", "TaxAmount", "VAT", "VATAmount", "TotalTax", "btw"],
        )
        invoice_items.append(
            {
                "Description_Dutch": str(description).replace("\n", " ") if description else "",
                "Amount": _safe_float(amount),
                "Tax_Amount": _safe_float(tax_amount),
            }
        )

    return invoice_items


def parse_azure_response(result: dict) -> dict:
    docs = result.get("analyzeResult", {}).get("documents", [])
    if not docs:
        logging.warning("Azure returned no document records.")
        return {
            "Invoice_Number": "NA",
            "Vendor_Name": "NA",
            "Property_Name": "NA",
            "Invoice_Date": None,
            "Amount": 0.0,
            "Tax_Amount": 0.0,
            "Description_Dutch": "",
            "IBAN": "NA",
            "Currency": "EUR",
            "InvoiceItems": [],
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
        "Invoice_Date": _lookup_field(fields, ["InvoiceDate", "Invoice Date", "date", "Date"]),
        "Amount": _safe_float(
            _lookup_field(fields, ["InvoiceTotal", "Invoice Total", "TotalAmount", "amount", "Amount"])
        ),
        "Tax_Amount": _safe_float(
            _lookup_field(fields, ["TotalTax", "VAT", "VATAmount", "Tax", "btw", "vat"])
        ),
        "Description_Dutch": " | ".join(descriptions),
        "IBAN": _lookup_field(fields, ["IBAN", "BankAccount", "Bank Account"]) or "NA",
        "Currency": _currency_code(fields),
        "InvoiceItems": invoice_items,
    }
