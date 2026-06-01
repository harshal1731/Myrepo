import io
import logging
import re
import threading
from difflib import SequenceMatcher
from typing import Any

import pandas as pd
try:
    from thefuzz import fuzz, process
except ImportError:  # pragma: no cover - production installs thefuzz from requirements.
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
        "vendorName",
        "TradingName",
        "Trading Name",
        "Vendor Name",
        "Vendor name",
        "PayeeName",
        "Payee Name",
        "Supplier Name",
    ],
    "PERSON": [
        "vendorId",
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
        "propertyId",
        "SiteCode",
        "Site Code",
        "Yardi Code",
        "Yardi code",
        "Property",
        "Property Code",
    ],
    "Property name": [
        "propertyName",
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
    "Unit Number": [
        "UnitNumber",
        "Unit Number",
        "Unit",
        "Apartment",
        "ApartmentNumber",
        "Apartment Number",
    ],
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


def _word_tokens(value: Any) -> set[str]:
    stop_words = {
        "bv",
        "b",
        "v",
        "cv",
        "c",
        "gp",
        "opco",
        "propco",
        "the",
        "and",
        "netherlands",
        "nederland",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 1 and token not in stop_words
    }


def _vendor_match_key(value: Any) -> str:
    tokens = sorted(_word_tokens(value))
    return " ".join(tokens) if tokens else _normalise_name(value)


def _has_vendor_token_similarity(query: str, candidate: str) -> bool:
    query_tokens = _word_tokens(query)
    candidate_tokens = _word_tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return False
    if query_tokens & candidate_tokens:
        return True
    return any(
        len(left) >= 5
        and len(right) >= 5
        and SequenceMatcher(None, left, right).ratio() >= 0.88
        for left in query_tokens
        for right in candidate_tokens
    )


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


def _is_missing_text(value: Any) -> bool:
    return _clean_text(value).upper() in {"", "NA", "N/A", "NONE", "NULL", "UNKNOWN", "NAN"}


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
    if vendor_df.empty or _is_missing_text(raw_vendor_name):
        return "NA", raw_vendor_name or "NA"

    choices: list[tuple[str, int, str]] = []
    for row_index, row in vendor_df.iterrows():
        for column in ("Vendor name", "PayeeName", "PERSON"):
            if column in vendor_df.columns:
                text = _clean_text(row.get(column))
                if text:
                    choices.append((_vendor_match_key(text), row_index, text))

    query_key = _vendor_match_key(raw_vendor_name)
    row_index, best_text, score = _best_match(
        query_key,
        [(choice, row_index) for choice, row_index, _ in choices],
        config.FUZZY_MATCH_THRESHOLD,
    )
    original_best = next((original for choice, _, original in choices if choice == best_text), best_text)
    logging.info("Vendor match query=%r best=%r score=%s", raw_vendor_name, best_text, score)
    if row_index is None or not _has_vendor_token_similarity(raw_vendor_name, original_best):
        return "NA", raw_vendor_name

    row = vendor_df.loc[row_index]
    person = _clean_text(row.get("PERSON"))
    return person or "NA", original_best


def get_vendor_person_code_from_text(raw_text: str) -> tuple[str, str]:
    with _DATA_LOCK:
        vendor_df = df_vendor

    cleaned_text = _clean_text(raw_text)
    if vendor_df.empty or _is_missing_text(cleaned_text):
        return "NA", "NA"

    normalised_text = _normalise_name(cleaned_text)
    matches: list[tuple[int, int, str, str]] = []
    for row_index, row in vendor_df.iterrows():
        for column in ("Vendor name", "PayeeName"):
            if column not in vendor_df.columns:
                continue
            vendor_name = _clean_text(row.get(column))
            normalised_vendor = _normalise_name(vendor_name)
            if len(normalised_vendor) >= 4 and normalised_vendor in normalised_text:
                matches.append((len(normalised_vendor), row_index, vendor_name, column))

    if not matches:
        return "NA", "NA"

    _, row_index, vendor_name, column = max(matches, key=lambda item: item[0])
    row = vendor_df.loc[row_index]
    person = _clean_text(row.get("PERSON"))
    logging.info(
        "Vendor text fallback matched vendor=%r column=%s person=%s",
        vendor_name,
        column,
        person or "NA",
    )
    return person or "NA", vendor_name or "NA"


def get_property_yardi_code(
    raw_property_name: str,
    *,
    unit_number: str = "",
    vendor_name: str = "",
) -> str:
    return get_property_match_details(
        raw_property_name,
        unit_number=unit_number,
        vendor_name=vendor_name,
    ).get("yardi_code", "NA")


def get_property_match_details(
    raw_property_name: str,
    *,
    unit_number: str = "",
    vendor_name: str = "",
) -> dict[str, str | int]:
    with _DATA_LOCK:
        master_df = df_master

    raw_property_name = _clean_text(raw_property_name)
    unit_number = _clean_text(unit_number)
    vendor_name = _clean_text(vendor_name)
    if master_df.empty:
        return {"yardi_code": "NA", "score": 0}

    if (
        vendor_name.lower().startswith("eteck incasso b.v")
        and unit_number
        and "Unit Number" in master_df.columns
    ):
        unit_rows = master_df[
            master_df["Unit Number"].astype(str).str.strip().str.lower()
            == unit_number.lower()
        ]
        if not unit_rows.empty:
            row = unit_rows.iloc[0]
            return _property_details_from_row(row, score=100)

    if _is_missing_text(raw_property_name):
        return {"yardi_code": "NA", "score": 0}

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

    exact_match = _exact_property_match(master_df, raw_property_name, match_columns)
    if exact_match is not None:
        return exact_match

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
        return {"yardi_code": "NA", "matched_text": best_text, "score": score}

    return _property_details_from_row(master_df.loc[row_index], score=score, matched_text=best_text)


def _exact_property_match(
    master_df: pd.DataFrame,
    raw_property_name: str,
    match_columns: list[str],
) -> dict[str, str | int] | None:
    query_norm = _normalise_name(raw_property_name)
    query_tokens = _word_tokens(raw_property_name)
    matches: list[tuple[int, int, int, str]] = []
    priority_by_column = {
        "Abbreviation": 0,
        "Yardi code": 1,
        "Property name": 2,
        "PropertyName1": 2,
        "PropertyName2": 2,
        "SiteName": 3,
        "BrandName": 3,
        "ProjectName": 3,
        "Site address": 4,
        "SiteAddress": 4,
    }

    for row_index, row in master_df.iterrows():
        for column in match_columns:
            if column not in master_df.columns:
                continue
            text = _clean_text(row.get(column))
            text_norm = _normalise_name(text)
            if len(text_norm) < 3:
                continue

            priority = priority_by_column.get(column, 5)
            if text_norm in query_norm:
                matches.append((priority, -len(text_norm), row_index, text))
                continue

            text_tokens = _word_tokens(text)
            if text_tokens and text_tokens.issubset(query_tokens):
                matches.append((priority, -sum(len(token) for token in text_tokens), row_index, text))

    if not matches:
        return None

    priority, _, row_index, matched_text = min(matches)
    logging.info(
        "Property exact match query=%r best=%r priority=%s",
        raw_property_name,
        matched_text,
        priority,
    )
    return _property_details_from_row(
        master_df.loc[row_index],
        score=100,
        matched_text=matched_text,
    )


def _property_details_from_row(
    row: pd.Series,
    *,
    score: int,
    matched_text: str = "",
) -> dict[str, str | int]:
    return {
        "yardi_code": _clean_text(row.get("Yardi code")) or "NA",
        "property_name": _clean_text(row.get("Property name")) or "NA",
        "site_name": _clean_text(row.get("SiteName")) or "NA",
        "site_address": _clean_text(row.get("Site address") or row.get("SiteAddress")) or "NA",
        "matched_text": matched_text or _clean_text(row.get("Property name")) or "NA",
        "score": score,
    }


def get_expense_account_code(
    english_desc: str,
    *,
    property_code: str = "",
    vendor_code: str = "",
    vendor_name: str = "",
) -> str:
    return get_expense_account_match(
        english_desc,
        property_code=property_code,
        vendor_code=vendor_code,
        vendor_name=vendor_name,
    ).get("account", "NA")


def get_expense_account_match(
    english_desc: str,
    *,
    property_code: str = "",
    vendor_code: str = "",
    vendor_name: str = "",
) -> dict[str, str | int]:
    with _DATA_LOCK:
        expense_df = df_expense

    english_desc = _clean_text(english_desc)
    if expense_df.empty or _is_missing_text(english_desc) or "AccountCode" not in expense_df.columns:
        return {"account": "NA", "notes": "NA", "score": 0}

    candidates: list[pd.DataFrame] = []
    combined_rows = pd.DataFrame()
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

    if vendor_name and vendor_name not in {"NA", "UNKNOWN"} and "PayeeName" in expense_df.columns:
        vendor_name_rows = expense_df[
            expense_df["PayeeName"].astype(str).str.lower() == vendor_name.lower()
        ]
        if not vendor_name_rows.empty:
            candidates.append(vendor_name_rows)

    candidates.append(expense_df)

    for working_df in candidates:
        match = _score_expense_accounts(working_df, english_desc)
        logging.info(
            "Expense account match account=%s score=%s notes=%r",
            match["account"],
            match["score"],
            match["notes"],
        )
        if int(match["score"]) >= config.EXPENSE_MATCH_THRESHOLD:
            return match

    if not combined_rows.empty:
        fallback_match = _most_common_expense_account(combined_rows)
        logging.info(
            "Expense account fallback account=%s notes=%r source=property+vendor-history",
            fallback_match["account"],
            fallback_match["notes"],
        )
        return fallback_match

    return {"account": "NA", "notes": "NA", "score": 0}


def _most_common_expense_account(working_df: pd.DataFrame) -> dict[str, str | int]:
    account_series = working_df["AccountCode"].map(_normalise_account_code)
    account_series = account_series[account_series.ne("NA")]
    if account_series.empty:
        return {"account": "NA", "notes": "NA", "score": 0}

    account = str(account_series.value_counts().idxmax())
    row = working_df[account_series.eq(account)].iloc[0]
    notes = _clean_text(row.get("Notes")) if "Notes" in working_df.columns else ""
    searchable = " ".join(
        _clean_text(row.get(column))
        for column in ("AccountName", "Notes", "PayeeName")
        if column in working_df.columns
    ).strip()
    return {"account": account, "notes": notes or searchable or "NA", "score": 64}


def _score_expense_accounts(working_df: pd.DataFrame, english_desc: str) -> dict[str, str | int]:
    best_score = -1
    best_account = "NA"
    best_notes = "NA"

    for _, row in working_df.iterrows():
        account = _normalise_account_code(row.get("AccountCode"))
        if account == "NA":
            continue

        notes = _clean_text(row.get("Notes")) if "Notes" in working_df.columns else ""
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
            best_notes = notes or searchable

    return {"account": best_account, "notes": best_notes or "NA", "score": best_score}
