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


def extract_invoice_data_from_memory(
    pdf_bytes: bytes,
    azure_key: str | None = None,
    azure_endpoint: str | None = None,
    azure_model_name: str | None = None,
) -> dict:
    if not pdf_bytes:
        raise AzureOcrError("PDF upload is empty.")

    subscription_key = _resolve_azure_key(azure_key)
    endpoint = _resolve_azure_endpoint(azure_endpoint)
    primary_model_name = _resolve_azure_model_name(azure_model_name)

    model_names = [
        name
        for name in (primary_model_name, config.AZURE_FALLBACK_MODEL_NAME)
        if name
    ]
    model_names = list(dict.fromkeys(model_names))
    errors: list[str] = []

    for index, model_name in enumerate(model_names):
        try:
            result = _analyze_with_model(pdf_bytes, model_name, subscription_key, endpoint)
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


def _resolve_azure_key(azure_key: str | None = None) -> str:
    key = str(azure_key).strip() if azure_key is not None else (config.AZURE_KEY or "").strip()
    if not key:
        raise AzureOcrError(
            "Azure OCR key was not provided. Pass azure-ocr-key as a "
            "multipart form field with the process-invoice request."
        )
    return key


def _resolve_azure_endpoint(azure_endpoint: str | None = None) -> str:
    endpoint = (
        str(azure_endpoint).strip()
        if azure_endpoint is not None
        else (config.AZURE_ENDPOINT or "").strip()
    )
    if not endpoint:
        raise AzureOcrError(
            "Azure OCR endpoint URL was not provided. Pass azure_url as a "
            "multipart form field with the process-invoice request."
        )
    if not re.match(r"^https?://", endpoint, re.IGNORECASE):
        raise AzureOcrError("Azure OCR endpoint URL must start with http:// or https://.")
    return endpoint.rstrip("/") + "/"


def _resolve_azure_model_name(azure_model_name: str | None = None) -> str:
    model_name = (
        str(azure_model_name).strip()
        if azure_model_name is not None
        else (config.AZURE_MODEL_NAME or "").strip()
    )
    if not model_name:
        raise AzureOcrError(
            "Azure OCR model name was not provided. Pass azure-model-name as a "
            "multipart form field with the process-invoice request."
        )
    return model_name


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


def _analyze_with_model(
    pdf_bytes: bytes,
    model_name: str,
    azure_key: str,
    azure_endpoint: str,
) -> dict:
    model_started = time.perf_counter()
    payload = {"base64Source": base64.b64encode(pdf_bytes).decode("utf-8")}
    headers = {
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": azure_key,
    }
    params = {
        "api-version": config.AZURE_API_VERSION,
        "stringIndexType": "textElements",
    }
    if config.AZURE_ANALYZE_PAGES:
        params["pages"] = config.AZURE_ANALYZE_PAGES
    post_url = (
        f"{azure_endpoint}documentintelligence/documentModels/"
        f"{model_name}:analyze"
    )

    logging.info(
        "Calling Azure model: %s pages=%s",
        model_name,
        params.get("pages", "all"),
    )
    submit_started = time.perf_counter()
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
    submit_seconds = time.perf_counter() - submit_started

    if response.status_code != 202:
        retryable = response.status_code not in {401, 403}
        logging.warning(
            "Azure model %s submit failed status=%s elapsed=%.2fs",
            model_name,
            response.status_code,
            submit_seconds,
        )
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
            f"{azure_endpoint}documentintelligence/documentModels/"
            f"{model_name}/analyzeResults/{request_id}"
        )
        get_params = {"api-version": config.AZURE_API_VERSION}
    else:
        raise AzureOcrError(
            "Azure response did not include an operation location.",
            retryable=True,
        )

    deadline = time.monotonic() + config.AZURE_POLL_TIMEOUT_SECONDS
    poll_started = time.perf_counter()
    poll_count = 0
    while time.monotonic() < deadline:
        if poll_count:
            time.sleep(config.AZURE_POLL_INTERVAL_SECONDS)
        poll_count += 1
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
            poll_seconds = time.perf_counter() - poll_started
            total_seconds = time.perf_counter() - model_started
            logging.info(
                "Azure model %s completed total=%.2fs submit=%.2fs poll=%.2fs poll_count=%s",
                model_name,
                total_seconds,
                submit_seconds,
                poll_seconds,
                poll_count,
            )
            return result
        if status == "failed":
            total_seconds = time.perf_counter() - model_started
            logging.warning(
                "Azure model %s failed after %.2fs poll_count=%s",
                model_name,
                total_seconds,
                poll_count,
            )
            raise AzureOcrError(
                f"Azure OCR processing failed: {result.get('error', result)}",
                retryable=True,
            )

    total_seconds = time.perf_counter() - model_started
    logging.warning(
        "Azure model %s timed out after %.2fs poll_count=%s",
        model_name,
        total_seconds,
        poll_count,
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


def _has_value(value: Any) -> bool:
    return value not in (None, "", "NA")


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
        r"\bNL\d{2}\s?[A-Z]{4}(?:\s?\d){10}\b|"
        r"\bGB\d{2}\s?[A-Z]{4}(?:\s?\d){14}\b|"
        r"\b(?:DE|FR|BE|ES|IT|IE|LU)\d{2}(?:\s?[A-Z0-9]){11,26}\b",
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


def _clean_ocr_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in str(text or "").splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]


_MONEY_PATTERN = re.compile(
    r"\(?[-+]?(?:[A-Z]{3}|[^\w\s])?\s*\d{1,3}(?:[.,]\d{3})*[.,]\d{2}\)?",
    re.IGNORECASE,
)
_DATE_TEXT_PATTERN = re.compile(
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|"
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}",
)
_KNOWN_LABEL_PATTERNS = (
    r"klantnr\.?",
    r"factuur\s*nr\.?",
    r"factuurnummer",
    r"factuurdatum",
    r"vervaldatum",
    r"orderdatum",
    r"order\s*nr\.?",
    r"nummer",
    r"nr\.?",
    r"artikel\s*nr\.?",
    r"datum",
    r"omschrijving",
    r"beschrijving",
    r"aantal",
    r"aantal/\s*eenh\.?",
    r"quantity",
    r"amount(?:\s+[A-Z]{3})?",
    r"rate",
    r"bedrag",
    r"prijs",
    r"prijs/\s*eenh\.?",
    r"netto\s+prijs",
    r"korting-\s*bedrag",
    r"stukprijs",
    r"btw\s*%",
    r"btw",
    r"vat",
    r"vat\s+amount(?:\s+[A-Z]{3})?",
    r"btw-bedrag",
    r"totaal",
    r"totaal\s+nettobedrag",
    r"totaal\s+excl\.?\s+btw",
    r"gesplitste/btw\s*-?code",
    r"btw\s+bedrag",
    r"totaal\s+incl\.?\s+btw",
    r"totale\s+btw",
    r"totaalbedrag",
    r"netto\s+totaal",
)


def _amounts_in_text(text: str) -> list[float]:
    return [_safe_float(match.group(0)) for match in _MONEY_PATTERN.finditer(text or "")]


def _money_texts_in_text(text: str) -> list[str]:
    return [match.group(0) for match in _MONEY_PATTERN.finditer(text or "")]


def _rate_texts_in_text(text: str) -> list[str]:
    return re.findall(r"\d{1,2}(?:[,.]\d+)?\s*%|zero\s+rated", text or "", re.IGNORECASE)


def _first_date_text(text: str) -> str | None:
    match = _DATE_TEXT_PATTERN.search(text or "")
    return match.group(0) if match else None


def _is_known_label(line: str) -> bool:
    clean = str(line or "").strip(" :")
    return any(re.fullmatch(pattern, clean, re.IGNORECASE) for pattern in _KNOWN_LABEL_PATTERNS)


def _label_block_value(lines: list[str], label_patterns: tuple[str, ...]) -> str | None:
    targets = [re.compile(pattern, re.IGNORECASE) for pattern in label_patterns]
    for start in range(len(lines)):
        if not _is_known_label(lines[start]):
            continue

        labels: list[str] = []
        index = start
        while index < len(lines) and len(labels) < 12 and _is_known_label(lines[index]):
            labels.append(lines[index])
            index += 1

        if not labels:
            continue

        for label_index, label in enumerate(labels):
            if not any(pattern.search(label) for pattern in targets):
                continue

            value_index = index + label_index
            if value_index < len(lines):
                return lines[value_index]

            if len(labels) == 1 and index < len(lines):
                return lines[index]
    return None


def _value_after_label(lines: list[str], label_pattern: str) -> str | None:
    pattern = re.compile(label_pattern, re.IGNORECASE)
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if not match:
            continue
        tail = line[match.end():].strip(" :-")
        if tail:
            return tail
        for candidate in lines[index + 1 : index + 4]:
            if candidate and not re.match(r"^[A-Za-z ]+:?$", candidate):
                return candidate
    return None


def _raw_invoice_number(lines: list[str]) -> str | None:
    value = _label_block_value(
        lines,
        (
            r"\binvoice\s*(?:number|no\.?|#)\b",
            r"\bfactuur\s*nr\.?\b",
            r"\bfactuurnummer\b",
            r"\bnummer\b",
        ),
    ) or _value_after_label(lines, r"\binvoice\s*(?:number|no\.?|#)\b|\bfactuurnummer\b|\bnummer\b")
    if value:
        return str(value).strip(" :")
    for line in lines:
        match = re.search(
            r"\bfactuur\s+([A-Z0-9][A-Z0-9 /.-]{3,})\b|"
            r"\b[A-Z]{1,4}\s*INV[-\s]?\d+\b|"
            r"\b\d{4}\s*/\s*\d+\b|"
            r"\bV\d{6,}\b",
            line,
            re.IGNORECASE,
        )
        if match:
            return (match.group(1) or match.group(0)).replace("Kenmerk", "").strip(" :")
    return None


def _raw_labeled_date(lines: list[str], labels: tuple[str, ...]) -> str | None:
    value = _label_block_value(lines, labels) or _value_after_label(lines, "|".join(labels))
    if value:
        return _first_date_text(value)
    return None


def _raw_total_amount(lines: list[str]) -> float | str:
    block_value = _label_block_value(
        lines,
        (
            r"totaalbedrag",
            r"totaal\s+incl\.?\s+btw",
            r"invoice\s+total",
            r"amount\s+due",
            r"te\s+betalen",
            r"totaal\s+te\s+betalen",
        ),
    )
    if block_value and _amounts_in_text(block_value):
        return _amounts_in_text(block_value)[-1]

    label_patterns = (
        r"invoice\s+total",
        r"amount\s+due",
        r"te\s+betalen",
        r"totaal\s+incl",
        r"totaalbedrag",
    )
    for index, line in enumerate(lines):
        if not any(re.search(pattern, line, re.IGNORECASE) for pattern in label_patterns):
            continue
        window = " ".join([line, *lines[index + 1 : index + 8]])
        amounts = _amounts_in_text(window)
        if amounts:
            return amounts[-1]

    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if re.fullmatch(r"totaal|total", line, re.IGNORECASE):
            amounts = _amounts_in_text(" ".join(lines[index + 1 : index + 4]))
            if amounts:
                return amounts[-1]
    return "NA"


def _raw_total_tax(lines: list[str]) -> float | str:
    block_value = _label_block_value(
        lines,
        (
            r"btw-bedrag",
            r"btw\s+bedrag",
            r"totale\s+btw",
            r"total\s+tax",
            r"vat\s+amount",
        ),
    )
    if block_value and _amounts_in_text(block_value):
        return _amounts_in_text(block_value)[-1]

    tax_amounts: list[float] = []
    for index, line in enumerate(lines):
        if re.search(r"^\s*totaal\s+(?:btw|vat)\b", line, re.IGNORECASE):
            continue
        if re.search(r"\b(?:btw|vat)\s*(?:nr|id|no)|\bNL\d{3,}", line, re.IGNORECASE):
            continue
        if re.search(r"\b(?:btw|vat)\s*\d{1,2}(?:[,.]\d+)?\s*%", line, re.IGNORECASE):
            window = " ".join(lines[index + 1 : index + 4])
            amounts = _amounts_in_text(window)
            if amounts:
                tax_amounts.append(amounts[0])
    if tax_amounts:
        return round(sum(tax_amounts), 2)

    if any(re.search(r"\bzero\s+rated\b", line, re.IGNORECASE) for line in lines):
        return 0.0
    return "NA"


def _raw_vendor_name(lines: list[str]) -> str | None:
    non_vendor_patterns = re.compile(
        r"\bfactuur\s+voor\b|\bt\.?a\.?v\.?\b|\banna van buerenplein\b|"
        r"\binvoice\s+(?:number|date)\b|\bdue\s+date\b|\bfactuur(?:nummer|datum)\b|"
        r"\bvervaldatum\b|^GS Netherlands\b|^OCO\b|^Orange House\b|^Opdrachtgever\b",
        re.IGNORECASE,
    )
    invoice_ref_pattern = re.compile(
        r"^(?:[A-Z]{1,4}\s*INV[-\s]?\d+|\d{4}\s*/\s*\d+|V\d{6,})$",
        re.IGNORECASE,
    )
    for line in lines[:100]:
        if non_vendor_patterns.search(line):
            continue
        if invoice_ref_pattern.match(line.strip()):
            continue
        match = re.search(r"\b[A-Z][A-Za-z0-9&.' -]+\s+(?:B\.?V\.?|Limited|Ltd\.?|Incasso B\.?V\.?)\b", line)
        if match and not re.search(r"\bGS Netherlands\b", match.group(0), re.IGNORECASE):
            return match.group(0).strip()
    for line in lines[:8]:
        if non_vendor_patterns.search(line):
            continue
        cleaned = re.sub(r"\b(?:FACTUUR|INVOICE)\b", "", line, flags=re.IGNORECASE).strip(" -")
        if len(cleaned) > 2 and not re.search(r"\bGLOBAL GROUP\b", cleaned, re.IGNORECASE):
            return cleaned
    return None


def _looks_like_vendor_name(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "UNKNOWN"}:
        return False
    if _first_date_text(text) and not re.search(r"[A-Za-z]{3,}", text):
        return False
    if re.fullmatch(r"[:\s\d./-]+", text):
        return False
    if re.search(r"\b(?:invoice|factuur|date|datum|due|vervaldatum)\b", text, re.IGNORECASE):
        return False
    return bool(re.search(r"[A-Za-z]{2,}", text))


def _raw_property_name(lines: list[str]) -> str | None:
    for line in lines[:40]:
        match = re.search(
            r"\b(?:GS Netherlands|OCO|Orange House)[A-Za-z0-9 .'-]*(?:B\.?V\.?|C\.?V\.?)\b",
            line,
            re.IGNORECASE,
        )
        if match:
            return match.group(0).strip()
    return None


def _is_money_line(line: str) -> bool:
    return bool(_MONEY_PATTERN.fullmatch(str(line or "").strip()))


def _is_quantity_line(line: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?(?:/[A-Za-z]+)?|[A-Za-z]+", line.strip()))


def _is_multi_quantity_line(line: str) -> bool:
    return len(re.findall(r"\d+(?:[.,]\d+)?", line or "")) > 1


def _is_item_number(line: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", line.strip()))


def _looks_like_sku(line: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{3,}", line.strip()))


def _looks_like_rate(line: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}(?:[,.]\d+)?\s*%|zero\s+rated", line.strip(), re.IGNORECASE))


def _line_tax_from_amount(amount: float | str, vat_rate_text: str) -> float | str:
    if amount == "NA":
        return "NA"
    if re.search(r"\bzero\s+rated\b|\b0\s*%", vat_rate_text or "", re.IGNORECASE):
        return 0.0
    match = re.search(r"(\d{1,2}(?:[,.]\d+)?)\s*%", vat_rate_text or "")
    if match:
        rate = float(match.group(1).replace(",", "."))
        return round(float(amount) * rate / 100, 2)
    return "NA"


def _append_item(
    rows: list[dict[str, Any]],
    description_parts: list[str],
    amount_text: str,
    vat_rate_text: str = "",
    tax_text: str = "",
) -> None:
    filtered_parts = [
        part
        for part in description_parts
        if not (
            len(description_parts) > 1
            and re.search(r"\bGL\s*\d|^periode\b|^period\b", part, re.IGNORECASE)
        )
    ]
    description = " ".join(part.strip() for part in filtered_parts if part.strip())
    if not description:
        return
    amount = _safe_float(amount_text)
    tax_amount = _safe_float(tax_text) if tax_text and _is_money_line(tax_text) else _line_tax_from_amount(amount, vat_rate_text)
    if amount == 0.0 and (tax_amount == "NA" or _safe_float(tax_amount) == 0.0):
        logging.info("Ignored zero-value OCR line item: %s", description)
        return
    rows.append(
        {
            "Description_Dutch": description,
            "Amount": amount,
            "Tax_Amount": tax_amount,
            "VAT_Rate": "0%" if re.search(r"\bzero\s+rated\b", vat_rate_text, re.IGNORECASE) else vat_rate_text or None,
        }
    )


def _split_compact_descriptions(description: str) -> list[str]:
    parts = [
        part.strip()
        for part in re.split(r"(?=\b\d{5,6}-\d{3}\s+-)", description or "")
        if part.strip()
    ]
    return parts or [description]


def _structured_items_low_quality(items: list[dict[str, Any]]) -> bool:
    if not items:
        return True
    if len(items) > 1:
        return False
    item = items[0]
    description = str(
        item.get("Description_Dutch")
        or item.get("Description_English")
        or item.get("Description")
        or item.get("Notes")
        or ""
    ).strip()
    return description in {"", "-", "NA"} or not _has_value(item.get("Tax_Amount"))


def _has_description_value(value: Any) -> bool:
    return str(value or "").strip() not in {"", "-", "NA", "UNKNOWN"}


def _raw_line_items(lines: list[str]) -> list[dict[str, Any]]:
    start = None
    for index, line in enumerate(lines):
        if re.fullmatch(r"services|omschrijving|beschrijving|description", line, re.IGNORECASE):
            start = index + 1
            break
    if start is None:
        return []

    while start < len(lines) and _is_known_label(lines[start]):
        start += 1

    stop_words = re.compile(r"^(subtotal|totaal|invoice total|amount due|vat rates|btw|for payment)\b", re.IGNORECASE)
    rows: list[dict[str, Any]] = []
    description_parts: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if stop_words.search(line):
            break

        if _is_multi_quantity_line(line) and description_parts and index + 2 < len(lines):
            compact_description = " ".join(description_parts)
            descriptions = _split_compact_descriptions(compact_description)
            amount_texts = _money_texts_in_text(lines[index + 1])
            rate_texts = _rate_texts_in_text(" ".join(lines[index + 2 : index + 5]))
            if len(descriptions) >= 2 and len(descriptions) == len(amount_texts):
                for item_index, description in enumerate(descriptions):
                    rate_text = rate_texts[item_index] if item_index < len(rate_texts) else ""
                    _append_item(rows, [description], amount_texts[item_index], rate_text)
                description_parts = []
                index += 5
                continue

        if _is_item_number(line) and index + 7 < len(lines) and _looks_like_sku(lines[index + 1]):
            desc = [lines[index + 2]]
            cursor = index + 3
            while cursor < len(lines) and not re.search(r"\d+(?:[.,]\d+)?/[A-Za-z]+", lines[cursor]):
                if not _is_money_line(lines[cursor]) and not _looks_like_rate(lines[cursor]):
                    desc.append(lines[cursor])
                cursor += 1
            if cursor + 4 < len(lines):
                net_total = lines[cursor + 3]
                vat_rate = lines[cursor + 4]
                if _is_money_line(net_total) and _looks_like_rate(vat_rate):
                    _append_item(rows, desc, net_total, vat_rate)
                    description_parts = []
                    index = cursor + 5
                    continue

        if _is_quantity_line(line) and description_parts:
            lookahead = lines[index + 1 : index + 6]
            if (
                len(lookahead) >= 3
                and _is_money_line(lookahead[0])
                and _is_money_line(lookahead[1])
                and _looks_like_rate(lookahead[2])
            ):
                _append_item(rows, description_parts, lookahead[1], lookahead[2])
                description_parts = []
                index += 4
                continue
            if (
                len(lookahead) >= 4
                and _is_money_line(lookahead[0])
                and _looks_like_rate(lookahead[1])
                and _is_money_line(lookahead[2])
                and _is_money_line(lookahead[3])
            ):
                _append_item(rows, description_parts, lookahead[3], lookahead[1], lookahead[2])
                description_parts = []
                index += 5
                continue
            if (
                len(lookahead) >= 3
                and _is_money_line(lookahead[0])
                and _looks_like_rate(lookahead[1])
                and _is_money_line(lookahead[2])
            ):
                _append_item(rows, description_parts, lookahead[2], lookahead[1])
                description_parts = []
                index += 4
                continue
            if (
                len(lookahead) >= 4
                and not _is_money_line(lookahead[0])
                and _looks_like_rate(lookahead[1])
                and _is_money_line(lookahead[2])
            ):
                _append_item(rows, description_parts, lookahead[2], lookahead[1])
                description_parts = []
                index += 4
                continue

        if re.match(r"^\d+\s+\D", line) and index + 3 < len(lines):
            desc = [re.sub(r"^\d+\s+", "", line)]
            if _is_money_line(lines[index + 1]) and _is_money_line(lines[index + 2]) and _looks_like_rate(lines[index + 3]):
                _append_item(rows, desc, lines[index + 2], lines[index + 3])
                index += 4
                continue

        if not _is_money_line(line):
            description_parts.append(line)
        index += 1
    return rows


def _apply_raw_text_fallbacks(parsed: dict[str, Any], raw_text: str) -> dict[str, Any]:
    lines = _clean_ocr_lines(raw_text)
    if not lines:
        return parsed

    fallbacks = {
        "Invoice_Number": _raw_invoice_number(lines),
        "Vendor_Name": _raw_vendor_name(lines),
        "Property_Name": _raw_property_name(lines),
        "Invoice_Date": _raw_labeled_date(lines, (r"\binvoice\s+date\b", r"\bfactuurdatum\b", r"\bdatum\b")),
        "Due_Date": _raw_labeled_date(lines, (r"\bdue\s+date\b", r"\bvervaldatum\b")),
        "Amount": _raw_total_amount(lines),
        "Tax_Amount": _raw_total_tax(lines),
    }
    for key, value in fallbacks.items():
        if not _has_value(parsed.get(key)) and _has_value(value):
            parsed[key] = value
            logging.info("Applied raw OCR fallback for %s.", key)

    if not _looks_like_vendor_name(parsed.get("Vendor_Name")):
        fallback_vendor = _raw_vendor_name(lines)
        parsed["Vendor_Name"] = fallback_vendor if _looks_like_vendor_name(fallback_vendor) else "NA"

    if not _has_description_value(parsed.get("Description_Dutch")):
        notes = _value_after_label(lines, r"\bnotes?\b|\bonderwerp\b|\bsubject\b")
        parsed["Description_Dutch"] = notes or parsed.get("Description_Dutch") or ""

    raw_line_items = _raw_line_items(lines)
    existing_items = parsed.get("InvoiceItems") or []
    if raw_line_items and _structured_items_low_quality(existing_items):
        parsed["InvoiceItems"] = raw_line_items
        logging.info("Parsed %s line item(s) from raw OCR text.", len(parsed["InvoiceItems"]))
    elif not existing_items:
        parsed["InvoiceItems"] = raw_line_items
        if raw_line_items:
            logging.info("Parsed %s line item(s) from raw OCR text.", len(raw_line_items))
        if not raw_line_items and _has_value(parsed.get("Amount")):
            parsed["InvoiceItems"] = [
                {
                    "Description_Dutch": parsed.get("Description_Dutch") or "Invoice total",
                    "Amount": parsed["Amount"],
                    "Tax_Amount": parsed.get("Tax_Amount") if _has_value(parsed.get("Tax_Amount")) else "NA",
                    "VAT_Rate": parsed.get("VAT_Rate"),
                }
            ]

    if not _has_description_value(parsed.get("Description_Dutch")) and parsed.get("InvoiceItems"):
        first_item = parsed["InvoiceItems"][0]
        if isinstance(first_item, dict):
            parsed["Description_Dutch"] = first_item.get("Description_Dutch") or parsed.get("Description_Dutch") or ""

    line_tax_sum = sum(
        _safe_float(item.get("Tax_Amount"))
        for item in parsed.get("InvoiceItems", []) or []
        if isinstance(item, dict) and _has_value(item.get("Tax_Amount"))
    )
    if line_tax_sum > 0:
        header_tax = parsed.get("Tax_Amount")
        if not _has_value(header_tax) or abs(_safe_float(header_tax) - line_tax_sum) > 0.01:
            parsed["Tax_Amount"] = round(line_tax_sum, 2)

    if parsed.get("IBAN") == "NA":
        parsed["IBAN"] = _iban_from_text(raw_text)

    return parsed


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
        parsed = {
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
        return _apply_raw_text_fallbacks(parsed, raw_text)

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

    parsed = {
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
    return _apply_raw_text_fallbacks(parsed, raw_text)
