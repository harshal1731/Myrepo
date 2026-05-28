# Greystar Invoice AI Microservice

FastAPI service for Greystar Netherlands Residential AP invoice processing.

The service receives master data, an expense distribution report, and PDF invoices. It keeps all uploaded data in memory, sends invoice PDFs to Azure Document Intelligence, translates invoice descriptions, maps the result to Yardi ETL fields, and returns a nested JSON payload.

## What This Service Does

1. Loads vendor and property master data from JSON into in-memory Pandas DataFrames.
2. Loads the expense distribution workbook into an in-memory Pandas DataFrame.
3. Sends invoice PDFs to Azure Document Intelligence.
4. Parses both structured Azure fields and raw OCR text.
5. Translates invoice descriptions where needed.
6. Maps property, vendor, offset, GL account, dates, VAT, payment type, and line items.
7. Returns the final Yardi JSON with `status` as either `Ready to Post` or `Review`.

No uploaded Excel or PDF file is saved to disk by the application.

## Code Map

| File | Responsibility |
|---|---|
| `app/main.py` | Creates the FastAPI app, configures logging, and includes routes. |
| `app/api/routes.py` | Defines the three API endpoints and orchestrates OCR, translation, and mapping. |
| `app/services/excel_service.py` | Loads master/expense data into RAM and performs vendor, property, and expense matching. |
| `app/services/ocr_service.py` | Calls Azure Document Intelligence and robustly parses structured fields plus raw OCR text. |
| `app/services/translation.py` | Translates Dutch invoice text with NLLB and guards against poor short-text translations. |
| `app/services/mapper_service.py` | Converts OCR data into the nested Yardi ETL response. |
| `app/models/schemas.py` | Pydantic response models for the final nested JSON shape. |
| `app/config.py` | Reads `.env` settings for Azure, model names, thresholds, logging, and timeouts. |
| `Dockerfile` | Production container startup using Uvicorn. |

## Runtime Data Flow

```text
.NET / tester
  |
  | POST /api/update-master-data
  v
Vendor + property master JSON
  |
  v
excel_service.df_vendor + excel_service.df_master

.NET / tester
  |
  | POST /api/upload-expense-report
  v
Expense Distribution Excel
  |
  v
excel_service.df_expense

.NET / tester
  |
  | POST /api/process-invoice
  v
PDF bytes in memory
  |
  v
Azure Document Intelligence
  |
  v
ocr_service.parse_azure_response()
  |
  v
translation.translate_dutch_to_english()
  |
  v
mapper_service.generate_yardi_payload()
  |
  v
Final Yardi JSON
```

## API Endpoints

### 1. `POST /api/update-master-data`

Loads dynamic master data from .NET into RAM.

Expected JSON shape:

```json
{
  "vendors": [
    {
      "RegisteredName": "nlqxlcas",
      "TradingName": "QX Limited"
    }
  ],
  "properties": [
    {
      "SiteCode": "nlodrb",
      "PropertyName1": "OurDomain Rotterdam Blaak",
      "PropertyName2": "OurDomain Rotterdam Blaak",
      "SiteName": "GS Netherlands Bright GP BV"
    }
  ]
}
```

Important key mappings:

| Incoming key | Internal column |
|---|---|
| `RegisteredName` | `PERSON` |
| `TradingName` | `Vendor name` |
| `SiteCode` | `Yardi code` |
| `PropertyName1`, `PropertyName2`, `SiteName` | `Property name` and property matching candidates |

### 2. `POST /api/upload-expense-report`

Loads the Expense Distribution workbook into RAM.

The service first tries `header=4`, which means row 5 is treated as the header. If the required columns are not found there, it falls back to `header=0` for compatibility with already-cleaned files.

Important columns:

| Expense column | Used for |
|---|---|
| `AccountCode` | Yardi `ACCOUNT` |
| `AccountName` | GL matching text |
| `Notes` | Line `NOTES` and GL matching text |
| `PayeeCode` | Vendor/person scoped matching |
| `PayeeName` | Vendor-name scoped matching |
| `Property` | Property scoped matching |

### 3. `POST /api/process-invoice`

Processes one PDF invoice and returns the final nested Yardi payload.

High-level route logic in `app/api/routes.py`:

1. Read uploaded PDF bytes into memory.
2. Call `extract_invoice_data_from_memory(pdf_bytes)`.
3. Translate the header description and each line description.
4. Call `generate_yardi_payload(ocr_data, file.filename)`.
5. Return a `YardiResponse`.

## OCR Flow

OCR is handled in `app/services/ocr_service.py`.

1. The PDF is base64 encoded in memory and sent to Azure.
2. The primary model is read from `AZURE_MODEL_NAME`.
3. `AZURE_FALLBACK_MODEL_NAME` is optional. Fallback is only used when this env var is non-empty.
4. The parser first reads Azure structured fields from `documents[0].fields`.
5. Then it applies raw OCR text fallbacks for fields that the custom model did not structure well.

The raw OCR fallback is generalized around invoice patterns:

- Label blocks, where Azure returns labels first and values below them.
- Common invoice labels such as invoice number, factuurdatum, vervaldatum, totaal, btw, VAT, amount due.
- Money formats such as `1.257,43`, `54.90`, `(2.59)`, and values with currency symbols.
- Dutch and English date formats.
- VAT rows such as `BTW 21%` followed by a tax amount.
- Table layouts where quantity, rate, VAT, and amount are separate OCR lines.
- Final standalone total labels, so a table-column `Totaal` is not mistaken for invoice grand total.
- Strict IBAN extraction to avoid swallowing nearby invoice words.
- Zero-value free items are ignored as non-posting lines.

## Translation Flow

Translation is handled in `app/services/translation.py`.

The service uses `facebook/nllb-200-distilled-600M` by default. It only translates text that looks like Dutch invoice text. English invoice text is returned unchanged.

Short invoice fragments can sometimes confuse machine translation, so the service validates translations using general confidence checks:

- Numbers from the source must survive in the translation.
- Short descriptions must preserve meaningful anchor tokens where appropriate.
- If the model output looks unrelated, the service falls back to a small invoice-domain glossary.

Example:

```text
Input:  uur beveiliging Canvas Utrecht in september 2025
Output: security hours Canvas Utrecht in September 2025
```

## Mapping Flow

Mapping is handled in `app/services/mapper_service.py`.

### Header fields

The service builds these Yardi header fields:

```text
PROPERTY, PERSON, OFFSET, DUEDATE, DATE, POSTMONTH, ACCRUAL, REF,
SEGMENT1..SEGMENT12, EXCHANGERATE, EXCHANGERATEDATE, TAXAMOUNT1, TAXAMOUNT2,
FROMDATE, TODATE, EXPENSETYPE, DETAILNOTES, DISPLAYTYPE,
ISCONSOLIDATECHECKS, DETAILVATTRANTYPEID, DETAILVATRATEID,
INTERNATIONALPAYMENTTYPE, InvoiceItems
```

Key mapping logic:

| Field | How it is filled |
|---|---|
| `PROPERTY` | Fuzzy/exact match invoice property text against in-memory property master. |
| `PERSON` | Fuzzy/exact match invoice vendor text against in-memory vendor master. |
| `OFFSET` | Static offset mapping by property Yardi code or entity/site name. |
| `DATE` | Invoice date, formatted as `DD/MM/YYYY`; defaults to `01/11/2025` only if missing. |
| `DUEDATE` | Invoice due date if present; otherwise invoice date + 30 days. |
| `POSTMONTH` | Invoice month as `MM/YYYY`; defaults to `12/2025` only if invoice date is missing. |
| `ACCRUAL` | Always `21010000`. |
| `REF` | Invoice number. |
| `FROMDATE`, `TODATE` | Extracted service period; if missing, defaults to invoice date. |
| `DETAILNOTES` | Translated invoice description. |
| `INTERNATIONALPAYMENTTYPE` | `Cheque` for non-EUR currency or GB IBAN; otherwise `EFT`. |

### Line items

Each line item contains:

```json
{
  "TRANNUM": 1,
  "ACCOUNT": "58205000",
  "NOTES": "Expense report note",
  "AMOUNT": 57.49,
  "DETAILTAXAMOUNT1": 0.0
}
```

Line rules:

- `TRANNUM` starts at 1 and increments per posting line.
- `AMOUNT` is the line net amount where available.
- `DETAILTAXAMOUNT1` is the line VAT amount.
- `ACCOUNT` is matched from the expense distribution report using property, vendor/person, vendor name, and invoice/line description.
- Discount/rebate lines inherit the previous posting line GL account when they are clearly connected to the previous charge.
- Zero amount and zero tax free lines are skipped because they are not Yardi posting lines.

## Status Rules

The response status is computed in `mapper_service._review_reasons()`.

`status = "Ready to Post"` when all required mapping fields are usable.

`status = "Review"` when any required value is missing or unavailable:

- Header: `PROPERTY`, `PERSON`, `OFFSET`, `REF`
- Line item: `ACCOUNT`, `AMOUNT`, `DETAILTAXAMOUNT1`
- `InvoiceItems` must exist and be a non-empty list.

Fields that are explicitly defaulted to `NA` do not cause Review:

```text
SEGMENT1..SEGMENT12, EXCHANGERATE, EXCHANGERATEDATE, TAXAMOUNT1, TAXAMOUNT2
```

## Example Happy Path

Example: `sample 2/7.pdf`

1. OCR extracts:
   - Vendor: `Endeavour B.V`
   - Property: `GS Netherlands Bright CV`
   - Invoice number: `20252309`
   - Invoice total: `1795.74`
2. Vendor matching maps the vendor to:
   - `PERSON = nldbopho`
3. Property matching maps Bright/ODRB to:
   - `PROPERTY = nlodrb`
4. Offset mapping maps the property/entity to:
   - `OFFSET = 11021-000`
5. Expense distribution matching creates four posting lines:
   - Social content management
   - Google CPC
   - Campaign management
   - Photography and video
6. Since all required fields are present, the final result is:

```json
{
  "vendor_file_name": "7.pdf - Endeavor Growth",
  "status": "Ready to Post",
  "etl_data": {
    "PROPERTY": "nlodrb",
    "PERSON": "nldbopho",
    "OFFSET": "11021-000",
    "REF": "20252309",
    "InvoiceItems": [
      {
        "TRANNUM": 1,
        "ACCOUNT": "54143001",
        "NOTES": "11/2024 Social Content Management",
        "AMOUNT": 367.02,
        "DETAILTAXAMOUNT1": 77.07
      }
    ]
  }
}
```

The real response includes all required header fields and all line items. The snippet above is shortened for readability.

## Example Review Path

Example: `Sample/2.pdf` or `Sample/3.pdf`

These invoices are parsed successfully, and the service maps:

```text
PROPERTY = nlcanvh
PERSON = nljangra or nlsecjva
REF = extracted invoice number
Line ACCOUNT/AMOUNT/TAX = present
```

They still return `Review` because the static `OFFSET_MAPPINGS` list does not contain `nlcanvh`.

To make those invoices `Ready to Post`, add the correct `nlcanvh` offset mapping in `app/services/mapper_service.py`.

Example: `sample 2/6.pdf`

This invoice returns `Review` because the OCR text does not contain a clear supplier/vendor name. It starts with the bill-to entity:

```text
GS Netherlands AMC Student C.V.
```

Since no vendor can be mapped to master data:

```text
PERSON = NA
InvoiceItems[1].ACCOUNT = NA
```

The account also remains unavailable because vendor/person context is missing for confident expense matching.

## Running Locally

From the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

API docs:

```text
http://127.0.0.1:8000/docs
```

Recommended test order:

1. Call `/api/update-master-data`.
2. Call `/api/upload-expense-report`.
3. Call `/api/process-invoice`.

The service is stateful only in RAM. If the server restarts, load master data and expense data again before processing invoices.

## Environment Variables

Create `.env` in the project root.

```text
AZURE_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
AZURE_KEY=<your-azure-key>
AZURE_MODEL_NAME=Greystar_common_logic_UK_v.1.3

# Optional. Leave blank to disable fallback.
AZURE_FALLBACK_MODEL_NAME=

AZURE_API_VERSION=2024-11-30
AZURE_POLL_INTERVAL_SECONDS=2
AZURE_POLL_TIMEOUT_SECONDS=120

NLLB_MODEL_NAME=facebook/nllb-200-distilled-600M
FUZZY_MATCH_THRESHOLD=80
EXPENSE_MATCH_THRESHOLD=65
LOG_TO_FILE=false
```

Fallback behavior:

- If `AZURE_FALLBACK_MODEL_NAME` is blank, only the Greystar custom model is used.
- If `AZURE_FALLBACK_MODEL_NAME=prebuilt-invoice`, the service can fallback when the custom model fails quality checks.

## Docker

Build:

```powershell
docker build -t greystar-invoice-api .
```

Run:

```powershell
docker run --env-file .env -p 8000:8000 greystar-invoice-api
```

The container starts with:

```text
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1}
```

For Azure App Service, keep `WEB_CONCURRENCY=1` unless you intentionally change the architecture. The master and expense data live in process memory, so multiple workers would each have separate RAM state.

## Logging

The app logs:

- Azure model used and OCR status.
- Raw OCR fallback fields applied.
- Parsed line item counts.
- Vendor/property fuzzy matches and scores.
- Expense account matches and fallback decisions.
- Final status and review reasons.

By default logs go to console. To also write logs to `logs/api_errors.log`:

```text
LOG_TO_FILE=true
```

## Important Design Notes

- Uploaded files are read into memory only.
- Pandas uses `io.BytesIO` for Excel loading.
- The global DataFrames are protected with a re-entrant lock during load/read paths.
- The API must be treated as RAM-stateful: after restart, load master data and expense report again.
- Review status is not an API failure. It means the JSON was produced, but at least one required business mapping needs human review or missing master/reference data.
