import io
import logging
import re
import threading
from typing import Any

import pandas as pd
try:
    from thefuzz import fuzz, process
except ImportError:  # pragma: no cover - production installs thefuzz from requirements.
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def WRatio(left: str, right: str) -> int:
            return _FallbackFuzz.token_set_ratio(left, right)

        @staticmethod
        def token_set_ratio(left: str, right: str) -> int:
            left_tokens = set(str(left).lower().split())
            right_tokens = set(str(right).lower().split())
            common = " ".join(sorted(left_tokens & right_tokens))
            left_extra = " ".join(sorted(left_tokens - right_tokens))
            right_extra = " ".join(sorted(right_tokens - left_tokens))
            left_joined = " ".join(part for part in (common, left_extra) if part)
            right_joined = " ".join(part for part in (common, right_extra) if part)
            return int(SequenceMatcher(None, left_joined, right_joined).ratio() * 100)

    class _FallbackProcess:
        @staticmethod
        def extractOne(query: str, choices: list[str], scorer=None):
            scorer = scorer or _FallbackFuzz.WRatio
            best_choice = None
            best_score = -1
            for choice in choices:
                score = scorer(query, choice)
                if score > best_score:
                    best_choice = choice
                    best_score = score
            return (best_choice, best_score) if best_choice is not None else None

    fuzz = _FallbackFuzz()
    process = _FallbackProcess()

from app import config

MASTER_VENDOR_SHEET = "Vendor name Phase -1"
MASTER_TRACKER_SHEET = "Master tracker (Entities)"

df_master = pd.DataFrame()
df_vendor = pd.DataFrame()
df_expense = pd.DataFrame()

_DATA_LOCK = threading.RLock()

_VENDOR_COLUMNS = {
    "Vendor name": [
        "TradingName",
        "Trading Name",
        "Vendor Name",
        "Vendor name",
        "PayeeName",
        "Payee Name",
        "Supplier Name",
    ],
    "PERSON": [
        "RegisteredName",
        "Registered Name",
        "PERSON",
        "Person",
        "PayeeCode",
        "Payee Code",
        "Vendor Code",
        "Yardi Vendor Code",
    ],
}

_PROPERTY_COLUMNS = {
    "Yardi code": [
        "SiteCode",
        "Site Code",
        "Yardi Code",
        "Yardi code",
        "Property",
        "Property Code",
    ],
    "Property name": [
        "PropertyName1",
        "Property Name 1",
        "Property name",
        "Property Name",
        "PropertyName2",
        "Property Name 2",
        "SiteName",
        "Site Name",
        "BrandName",
        "Brand Name",
    ],
    "Site address": ["SiteAddress", "Site Address", "Address", "Property Address"],
    "Head office": ["HeadOffice", "Head office", "Head Office"],
    "Unit Size": ["UnitSize", "Unit Size"],
}

_EXPENSE_COLUMNS = {
    "AccountCode": ["AccountCode", "Account Code", "ACCOUNT", "Account"],
    "AccountName": ["AccountName", "Account Name", "Account Description"],
    "Notes": ["Notes", "Description", "Narrative", "DETAILNOTES"],
    "PayeeName": ["PayeeName", "Payee Name", "Vendor name", "Vendor Name"],
    "PayeeCode": ["PayeeCode", "Payee Code", "PERSON", "Person"],
    "Property": ["Property", "Yardi code", "Yardi Code", "Property Code"],
}


def _normalise_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def _has_value(series: pd.Series) -> pd.Series:
    return series.notna() & series.astype(str).str.strip().ne("")


def _canonicalise_columns(df: pd.DataFrame, aliases: dict[str, list[str]]) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(col).strip() for col in df.columns]
    lookup = {_normalise_name(col): col for col in df.columns}

    for target, names in aliases.items():
        merged = None
        for name in [target, *names]:
            source = lookup.get(_normalise_name(name))
            if source is None:
                continue
            candidate = df[source]
            if merged is None:
                merged = candidate.copy()
            else:
                merged = merged.where(_has_value(merged), candidate)
        if merged is not None:
            df[target] = merged

    return df


def _clean_excel_df(df: pd.DataFrame, *, forward_fill: bool = False) -> pd.DataFrame:
    df = df.replace(r"^\s*$", pd.NA, regex=True)
    df = df.dropna(how="all")
    if forward_fill and not df.empty:
        df = df.ffill()
    return df.reset_index(drop=True)


def _validate_sheet_names(workbook: pd.ExcelFile) -> None:
    missing = [
        sheet
        for sheet in (MASTER_VENDOR_SHEET, MASTER_TRACKER_SHEET)
        if sheet not in workbook.sheet_names
    ]
    if missing:
        raise ValueError(
            "Master file is missing required sheet(s): " + ", ".join(missing)
        )


def load_master_data_from_memory(file_bytes: bytes) -> dict[str, int]:
    """Load the two master workbook sheets into RAM-only pandas DataFrames."""
    global df_master, df_vendor

    with io.BytesIO(file_bytes) as stream:
        workbook = pd.ExcelFile(stream)
        _validate_sheet_names(workbook)
        vendor_df = pd.read_excel(workbook, sheet_name=MASTER_VENDOR_SHEET, dtype=object)
        master_df = pd.read_excel(workbook, sheet_name=MASTER_TRACKER_SHEET, dtype=object)

    vendor_df = _canonicalise_columns(
        _clean_excel_df(vendor_df, forward_fill=True),
        _VENDOR_COLUMNS,
    )
    master_df = _canonicalise_columns(
        _clean_excel_df(master_df, forward_fill=True),
        _PROPERTY_COLUMNS,
    )

    if "PERSON" not in vendor_df or "Vendor name" not in vendor_df:
        raise ValueError("Vendor sheet must contain vendor name and PERSON code columns.")
    if "Yardi code" not in master_df or "Property name" not in master_df:
        raise ValueError("Master tracker sheet must contain property name and Yardi code columns.")

    with _DATA_LOCK:
        df_vendor = vendor_df
        df_master = master_df

    logging.info(
        "Loaded master workbook into RAM. Vendors=%s Properties=%s",
        len(vendor_df),
        len(master_df),
    )
    return {"vendors": len(vendor_df), "properties": len(master_df)}


def load_master_data_from_json(payload: dict) -> dict[str, int]:
    """Compatibility path for .NET JSON master data; still stored only in RAM."""
    global df_master, df_vendor

    vendor_df = _canonicalise_columns(
        _clean_excel_df(pd.DataFrame(payload.get("vendors", []))),
        _VENDOR_COLUMNS,
    )
    master_df = _canonicalise_columns(
        _clean_excel_df(pd.DataFrame(payload.get("properties", []))),
        _PROPERTY_COLUMNS,
    )

    if vendor_df.empty or "PERSON" not in vendor_df or "Vendor name" not in vendor_df:
        raise ValueError("JSON vendors must include RegisteredName and TradingName values.")
    if master_df.empty or "Yardi code" not in master_df or "Property name" not in master_df:
        raise ValueError("JSON properties must include SiteCode and PropertyName1 values.")

    with _DATA_LOCK:
        df_vendor = vendor_df
        df_master = master_df

    logging.info(
        "Loaded JSON master data into RAM. Vendors=%s Properties=%s",
        len(vendor_df),
        len(master_df),
    )
    return {"vendors": len(vendor_df), "properties": len(master_df)}


def _expense_shape_is_valid(df: pd.DataFrame) -> bool:
    columns = {_normalise_name(col) for col in df.columns}
    return _normalise_name("AccountCode") in columns and (
        _normalise_name("Notes") in columns or _normalise_name("AccountName") in columns
    )


def load_expense_report_from_memory(file_bytes: bytes) -> dict[str, int | str]:
    """Load Expense Distribution into RAM, preferring the required row-5 header."""
    global df_expense

    candidates: list[tuple[int, pd.DataFrame]] = []
    for header_row in (4, 0):
        with io.BytesIO(file_bytes) as stream:
            candidate = pd.read_excel(stream, header=header_row, dtype=object)
        candidate = _canonicalise_columns(_clean_excel_df(candidate), _EXPENSE_COLUMNS)
        candidates.append((header_row, candidate))
        if _expense_shape_is_valid(candidate):
            break

    header_row, expense_df = candidates[-1]
    if not _expense_shape_is_valid(expense_df):
        raise ValueError(
            "Expense report must contain AccountCode and at least one text column "
            "(Notes or AccountName)."
        )

    with _DATA_LOCK:
        df_expense = expense_df

    logging.info(
        "Loaded expense report into RAM. Rows=%s HeaderRow=%s",
        len(expense_df),
        header_row + 1,
    )
    return {"rows": len(expense_df), "header_row": f"{header_row + 1}"}


def _clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _normalise_account_code(value: Any) -> str:
    if value is None or pd.isna(value):
        return "NA"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = re.sub(r"[^A-Za-z0-9]", "", str(value).strip())
    return text or "NA"


def _best_match(query: str, choices: list[tuple[str, int]], threshold: int) -> tuple[int | None, str, int]:
    if not query or not choices:
        return None, query, 0

    choice_text = [item[0] for item in choices]
    match = process.extractOne(query, choice_text, scorer=fuzz.WRatio)
    if not match:
        return None, query, 0

    best_text, score = match[0], int(match[1])
    if score < threshold:
        return None, best_text, score

    for text, row_index in choices:
        if text == best_text:
            return row_index, best_text, score

    return None, best_text, score


def get_vendor_person_code(raw_vendor_name: str) -> tuple[str, str]:
    with _DATA_LOCK:
        vendor_df = df_vendor

    raw_vendor_name = _clean_text(raw_vendor_name)
    if vendor_df.empty or not raw_vendor_name:
        return "NA", raw_vendor_name or "NA"

    choices: list[tuple[str, int]] = []
    for row_index, row in vendor_df.iterrows():
        for column in ("Vendor name", "PayeeName", "PERSON"):
            if column in vendor_df.columns:
                text = _clean_text(row.get(column))
                if text:
                    choices.append((text, row_index))

    row_index, best_text, score = _best_match(
        raw_vendor_name,
        choices,
        config.FUZZY_MATCH_THRESHOLD,
    )
    logging.info("Vendor match query=%r best=%r score=%s", raw_vendor_name, best_text, score)
    if row_index is None:
        return "NA", raw_vendor_name

    row = vendor_df.loc[row_index]
    person = _clean_text(row.get("PERSON"))
    return person or "NA", best_text


def get_property_yardi_code(raw_property_name: str) -> str:
    with _DATA_LOCK:
        master_df = df_master

    raw_property_name = _clean_text(raw_property_name)
    if master_df.empty or not raw_property_name:
        return "NA"

    match_columns = [
        "Property name",
        "PropertyName1",
        "PropertyName2",
        "SiteName",
        "Site address",
        "SiteAddress",
        "BrandName",
        "ProjectName",
        "Abbreviation",
        "Yardi code",
    ]

    choices: list[tuple[str, int]] = []
    for row_index, row in master_df.iterrows():
        for column in match_columns:
            if column in master_df.columns:
                text = _clean_text(row.get(column))
                if text:
                    choices.append((text, row_index))

    row_index, best_text, score = _best_match(
        raw_property_name,
        choices,
        config.FUZZY_MATCH_THRESHOLD,
    )
    logging.info("Property match query=%r best=%r score=%s", raw_property_name, best_text, score)
    if row_index is None:
        return "NA"

    yardi_code = _clean_text(master_df.loc[row_index].get("Yardi code"))
    return yardi_code or "NA"


def get_expense_account_code(
    english_desc: str,
    *,
    property_code: str = "",
    vendor_code: str = "",
) -> str:
    with _DATA_LOCK:
        expense_df = df_expense

    english_desc = _clean_text(english_desc)
    if expense_df.empty or not english_desc or "AccountCode" not in expense_df.columns:
        return "NA"

    candidates: list[pd.DataFrame] = []
    if property_code and property_code not in {"NA", "UNKNOWN"} and "Property" in expense_df.columns:
        property_rows = expense_df[
            expense_df["Property"].astype(str).str.lower() == property_code.lower()
        ]
        if not property_rows.empty:
            candidates.append(property_rows)

    if vendor_code and vendor_code not in {"NA", "UNKNOWN"} and "PayeeCode" in expense_df.columns:
        vendor_rows = expense_df[
            expense_df["PayeeCode"].astype(str).str.lower() == vendor_code.lower()
        ]
        if not vendor_rows.empty:
            candidates.append(vendor_rows)

    if (
        property_code
        and property_code not in {"NA", "UNKNOWN"}
        and vendor_code
        and vendor_code not in {"NA", "UNKNOWN"}
        and {"Property", "PayeeCode"}.issubset(expense_df.columns)
    ):
        combined_rows = expense_df[
            (expense_df["Property"].astype(str).str.lower() == property_code.lower())
            & (expense_df["PayeeCode"].astype(str).str.lower() == vendor_code.lower())
        ]
        if not combined_rows.empty:
            candidates.insert(0, combined_rows)

    candidates.append(expense_df)

    for working_df in candidates:
        best_account, best_score = _score_expense_accounts(working_df, english_desc)
        logging.info("Expense account match account=%s score=%s", best_account, best_score)
        if best_score >= config.EXPENSE_MATCH_THRESHOLD:
            return best_account

    return "NA"


def _score_expense_accounts(working_df: pd.DataFrame, english_desc: str) -> tuple[str, int]:
    best_score = -1
    best_account = "NA"

    for _, row in working_df.iterrows():
        account = _normalise_account_code(row.get("AccountCode"))
        if account == "NA":
            continue

        searchable = " ".join(
            _clean_text(row.get(column))
            for column in ("AccountName", "Notes", "PayeeName")
            if column in working_df.columns
        ).strip()
        if not searchable:
            continue

        score = fuzz.token_set_ratio(english_desc.lower(), searchable.lower())
        if score > best_score:
            best_score = score
            best_account = account

    return best_account, best_score
