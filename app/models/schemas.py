from pydantic import BaseModel
from typing import Any, List, Union


class InvoiceItem(BaseModel):
    TRANNUM: int
    ACCOUNT: str
    NOTES: str
    AMOUNT: Union[float, str]
    DETAILTAXAMOUNT1: Union[float, str]


class YardiEtlData(BaseModel):
    PROPERTY: str
    PERSON: str
    OFFSET: str
    DUEDATE: str
    DATE: str
    POSTMONTH: str
    ACCRUAL: str
    REF: str
    SEGMENT1: str
    SEGMENT2: str
    SEGMENT3: str
    SEGMENT4: str
    SEGMENT5: str
    SEGMENT6: str
    SEGMENT7: str
    SEGMENT8: str
    SEGMENT9: str
    SEGMENT10: str
    SEGMENT11: str
    SEGMENT12: str
    EXCHANGERATE: str
    EXCHANGERATEDATE: str
    TAXAMOUNT1: str
    TAXAMOUNT2: str
    FROMDATE: str
    TODATE: str
    EXPENSETYPE: str
    DETAILNOTES: str
    DISPLAYTYPE: str
    ISCONSOLIDATECHECKS: int
    DETAILVATTRANTYPEID: str
    DETAILVATRATEID: str
    INTERNATIONALPAYMENTTYPE: str
    InvoiceItems: List[InvoiceItem]


class YardiResponse(BaseModel):
    vendor_file_name: str
    status: str
    etl_data: YardiEtlData

# New schemas for the JSON Master Data payload from .NET
class MasterDataPayload(BaseModel):
    vendors: List[dict[str, Any]]
    properties: List[dict[str, Any]]
