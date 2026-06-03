# Greystar Invoice AI Microservice

FastAPI service for Greystar Netherlands Residential AP invoice processing.

The service loads master data and expense distribution data into RAM, sends PDF invoices to Azure Document Intelligence, parses OCR results, translates descriptions where needed, maps the data to Yardi ETL fields, and returns a posting payload.

Uploaded Excel/PDF files are read from memory streams only. The application does not save uploaded files to disk.

## Main Workflow

```text
1. POST /api/update-master-data
   Load vendor + property master JSON into RAM.

2. POST /api/upload-expense-report
   Load Expense Distribution Excel into RAM.

3. POST /api/process-invoice
   OCR PDF -> translate descriptions -> map to Yardi JSON.

4. POST /api/update-invoice
   Validate a UI-edited Yardi JSON without rerunning OCR.
```

The service keeps master and expense data in global Pandas DataFrames. If the app restarts, load master data and expense data again before processing invoices.

## Code Map

| File | What it does |
|---|---|
| `app/main.py` | Creates the FastAPI app and logging setup. |
| `app/api/routes.py` | Defines API endpoints and orchestrates service calls. |
| `app/services/excel_service.py` | Loads master/expense data and performs property, vendor, and expense matching. |
| `app/services/ocr_service.py` | Calls Azure Document Intelligence and parses structured fields plus raw OCR text. |
| `app/services/translation.py` | Translates Dutch invoice descriptions and protects against poor short-text translations. |
| `app/services/mapper_service.py` | Builds the final Yardi ETL JSON and computes posting status. |
| `app/models/schemas.py` | Pydantic request/response schemas. |
| `app/config.py` | Reads environment variables. |

## Endpoint 1: Update Master Data

`POST /api/update-master-data`

Content-Type:

```text
application/json
```

Request body rough schema:

```json
{
  "vendors": [
    {
      "vendorId": "VND101",
      "vendorName": "Example Vendor BV"
    }
  ],
  "properties": [
    {
      "propertyId": "PROP902",
      "propertyName": "Amsterdam Office Suite"
    }
  ]
}
```

The code also supports the .NET/Yardi names used in the project:

| Incoming key | Internal meaning |
|---|---|
| `vendorId` or `RegisteredName` | `PERSON` |
| `vendorName` or `TradingName` | Vendor name |
| `propertyId` or `SiteCode` | Yardi property code |
| `propertyName`, `PropertyName1`, `PropertyName2`, `SiteName` | Property matching text |

Success response:

```json
{
  "status": true,
  "message": "Master data (Vendors & Properties) loaded into RAM successfully.",
  "loaded": {
    "vendorsCount": 1,
    "propertiesCount": 1
  }
}
```

Failure response uses `status: false` with a message explaining why the JSON could not be loaded.

## Endpoint 2: Upload Expense Report

`POST /api/upload-expense-report`

Content-Type:

```text
multipart/form-data
```

Multipart form parameter:

```text
expense_file: optional .xls/.xlsx file
```

If a file is provided:

```json
{
  "status": true,
  "message": "Uploaded Excel data loaded into RAM successfully.",
  "loaded": {
    "expense_file": {
      "rows_processed": 120,
      "status": true
    }
  }
}
```

If no file is provided:

```json
{
  "status": false,
  "message": "No expense_file supplied. Existing in-memory expense data was unchanged.",
  "loaded": {}
}
```

The loader first tries Excel row 5 as the header (`header=4`). If the expected columns are not found, it falls back to row 1 (`header=0`) for already-cleaned workbooks.

## Endpoint 3: Process Invoice

`POST /api/process-invoice`

Content-Type:

```text
multipart/form-data
```

Multipart form parameter:

```text
file: required .pdf file
azure-ocr-key: required Azure Document Intelligence key
azure_url: required Azure Document Intelligence endpoint URL
```

`azure-ocr-key` and `azure_url` must be sent as multipart form fields. The API
does not read Azure credentials from request headers.

Response rough schema:

```json
{
  "vendor_file_name": "Factuur_2025-0293.pdf - Securo Beveiliging B.V.",
  "InvoiceStatus": "Ready to Post",
  "status": true,
  "message": "Ready to Post",
  "etl_data": {
    "PROPERTY": "nlcanvt",
    "PERSON": "nlsecjva",
    "OFFSET": "11020000",
    "DUEDATE": "31/10/2025",
    "DATE": "01/10/2025",
    "POSTMONTH": "10/2025",
    "ACCOUNT": "52190000",
    "ACCRUAL": "21010000",
    "REF": "2025-0293",
    "SEGMENT1": "None",
    "SEGMENT2": "None",
    "SEGMENT3": "None",
    "SEGMENT4": "None",
    "SEGMENT5": "None",
    "SEGMENT6": "None",
    "SEGMENT7": "None",
    "SEGMENT8": "None",
    "SEGMENT9": "None",
    "SEGMENT10": "None",
    "SEGMENT11": "None",
    "SEGMENT12": "None",
    "EXCHANGERATE": "None",
    "EXCHANGERATEDATE": "None",
    "TAXAMOUNT1": "None",
    "TAXAMOUNT2": "None",
    "FROMDATE": "01/10/2025",
    "TODATE": "01/10/2025",
    "EXPENSETYPE": "Expense (Opex)",
    "DETAILNOTES": "512 hours of security Canvas Utrecht in September 2025",
    "DISPLAYTYPE": "NLVatApplicablePayable",
    "ISCONSOLIDATECHECKS": -1,
    "DETAILVATTRANTYPEID": "apresex",
    "DETAILVATRATEID": "Uksr",
    "INTERNATIONALPAYMENTTYPE": "EFT",
    "InvoiceItems": [
      {
        "TRANNUM": 1,
        "ACCOUNT": "52190000",
        "NOTES": "512 hours of security Canvas Utrecht in September 2025",
        "AMOUNT": 22528.0,
        "DETAILTAXAMOUNT1": 4730.88
      }
    ]
  }
}
```

`ACCOUNT` is present at header level for UI/summary use. Line-level `ACCOUNT` is also kept because multi-line invoices can have different GL accounts.

## Endpoint 4: Update Invoice

`POST /api/update-invoice`

Content-Type:

```text
application/json
```

Purpose:

Use this when the UI or .NET team sends back an edited invoice JSON. This endpoint does not call Azure and does not translate again. It only validates the updated payload with the same posting rules used by `/api/process-invoice`.

Input:

```text
Same JSON shape returned by /api/process-invoice
```

Output:

```json
{
  "InvoiceStatus": "Ready to Post",
  "status": true,
  "message": "Ready to Post"
}
```

If a required field is still missing:

```json
{
  "InvoiceStatus": "Review",
  "status": false,
  "message": "Vendor not available, Expense account not available"
}
```

## OCR Flow

`app/services/ocr_service.py` handles OCR.

1. PDF bytes are base64 encoded in memory.
2. Azure Document Intelligence is called using `AZURE_MODEL_NAME`.
   The request-level `azure-ocr-key` and `azure_url` form fields are used for
   this call.
3. `AZURE_FALLBACK_MODEL_NAME` is optional and only used when set.
4. The parser reads structured fields from Azure.
5. Raw OCR text fallbacks fill missing or poorly structured fields.

The service logs OCR timing for each processed invoice, including Azure model
submit time, poll time, poll count, OCR total time, translation time, mapping
time, and full request time.

The parser is generalized around invoice patterns:

- Label/value blocks.
- Dutch and English invoice labels.
- Money formats such as `1.257,43`, `54.90`, and `(2.59)`.
- VAT rows such as `BTW 21%`.
- Table layouts where quantity, rate, VAT, and amount are on separate OCR lines.
- Final invoice total detection.
- Strict IBAN extraction.
- Zero-value free items are ignored as non-posting lines.

## Mapping Flow

`app/services/mapper_service.py` builds the final Yardi JSON.

Important mapping rules:

| Field | Rule |
|---|---|
| `PROPERTY` | Match invoice property/site text against loaded property master. |
| `PERSON` | Match invoice vendor against loaded vendor master. |
| `OFFSET` | Match property/entity against static `OFFSET_MAPPINGS`. |
| `DATE` | Invoice date as `DD/MM/YYYY`; default is `01/11/2025` if missing. |
| `DUEDATE` | Invoice due date if available; otherwise invoice date + 30 days. |
| `POSTMONTH` | Invoice month as `MM/YYYY`; default is `12/2025` if invoice date is missing. |
| `ACCOUNT` | Matched from Expense Distribution using vendor, property, and description context. |
| `DETAILNOTES` | Translated invoice description. |
| `INTERNATIONALPAYMENTTYPE` | `Cheque` for non-EUR or GB IBAN; otherwise `EFT`. |

Status is calculated from required fields:

- Header required: `PROPERTY`, `PERSON`, `OFFSET`, `REF`
- Line required: `ACCOUNT`, `AMOUNT`, `DETAILTAXAMOUNT1`
- `InvoiceItems` must be present and non-empty.

If all required fields are valid:

```json
{
  "InvoiceStatus": "Ready to Post",
  "status": true,
  "message": "Ready to Post"
}
```

If something is missing:

```json
{
  "InvoiceStatus": "Review",
  "status": false,
  "message": "Vendor not available"
}
```

Unavailable extracted, mapped, or default placeholder ETL values are returned as
the string `"None"`. Default placeholder fields like `SEGMENT1..SEGMENT12`,
`EXCHANGERATE`, `EXCHANGERATEDATE`, `TAXAMOUNT1`, and `TAXAMOUNT2` do not
trigger Review.

## Example Flow

Happy path example:

1. `/api/update-master-data` loads vendors and properties into RAM.
2. `/api/upload-expense-report` loads expense history into RAM.
3. `/api/process-invoice` receives `7.pdf`.
4. OCR extracts vendor `Endeavour B.V`, property `GS Netherlands Bright CV`, and invoice reference `20252309`.
5. Vendor matching maps to `PERSON = nldbopho`.
6. Property matching maps to `PROPERTY = nlodrb`.
7. Offset mapping maps to `OFFSET = 11021-000`.
8. Expense matching creates four invoice lines with GL accounts.
9. Required fields are complete, so response is `InvoiceStatus = Ready to Post` and `status = true`.

Review example:

If OCR finds a property and amount but cannot find a vendor in master data:

```json
{
  "InvoiceStatus": "Review",
  "status": false,
  "message": "Vendor not available, Expense account not available"
}
```

The UI can let a user correct `PERSON` and `ACCOUNT`, then send the edited JSON to `/api/update-invoice`. If the edited fields are valid, `/api/update-invoice` returns `Ready to Post`.

## Running Locally

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

API docs:

```text
http://127.0.0.1:8000/docs
```

Recommended manual test order:

1. `POST /api/update-master-data`
2. `POST /api/upload-expense-report`
3. `POST /api/process-invoice`
4. Optional: `POST /api/update-invoice`

## Environment Variables

Create `.env` in the project root:

```text
AZURE_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
# /api/process-invoice expects azure-ocr-key and azure_url as multipart form-data.
AZURE_KEY=<optional-local-fallback-key>
AZURE_MODEL_NAME=Greystar_common_logic_UK_v.1.3

# Leave blank to disable fallback.
AZURE_FALLBACK_MODEL_NAME=

AZURE_API_VERSION=2024-11-30
AZURE_POLL_INTERVAL_SECONDS=2
AZURE_POLL_TIMEOUT_SECONDS=120

NLLB_MODEL_NAME=facebook/nllb-200-distilled-600M
FUZZY_MATCH_THRESHOLD=80
EXPENSE_MATCH_THRESHOLD=65
LOG_TO_FILE=false
```

## Docker

Build:

```powershell
docker build -t greystar-invoice-api .
```

Run:

```powershell
docker run --env-file .env -p 8000:8000 greystar-invoice-api
```

The container starts with Uvicorn on `0.0.0.0` using `${PORT:-8000}`.

Use `WEB_CONCURRENCY=1` unless the architecture changes. The loaded master and expense data live in process memory, so multiple workers would have separate RAM state.
