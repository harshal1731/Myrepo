from pydantic import BaseModel
from typing import Any, List, Optional, Union


class InvoiceItem(BaseModel):
    TRANNUM: int
    ACCOUNT: Optional[str] = None
    NOTES: Optional[str] = None
    AMOUNT: Union[float, str, None]
    DETAILTAXAMOUNT1: Union[float, str, None]


class YardiEtlData(BaseModel):
    PROPERTY: Optional[str]
    PERSON: Optional[str]
    OFFSET: Optional[str]
    DUEDATE: Optional[str]
    DATE: Optional[str]
    POSTMONTH: Optional[str]
    ACCOUNT: Optional[str] = None
    ACCRUAL: str
    REF: Optional[str]
    SEGMENT1: Optional[str]
    SEGMENT2: Optional[str]
    SEGMENT3: Optional[str]
    SEGMENT4: Optional[str]
    SEGMENT5: Optional[str]
    SEGMENT6: Optional[str]
    SEGMENT7: Optional[str]
    SEGMENT8: Optional[str]
    SEGMENT9: Optional[str]
    SEGMENT10: Optional[str]
    SEGMENT11: Optional[str]
    SEGMENT12: Optional[str]
    EXCHANGERATE: Optional[str]
    EXCHANGERATEDATE: Optional[str]
    TAXAMOUNT1: Optional[str]
    TAXAMOUNT2: Optional[str]
    FROMDATE: Optional[str]
    TODATE: Optional[str]
    EXPENSETYPE: str
    DETAILNOTES: Optional[str]
    DISPLAYTYPE: str
    ISCONSOLIDATECHECKS: int
    DETAILVATTRANTYPEID: str
    DETAILVATRATEID: Optional[str]
    INTERNATIONALPAYMENTTYPE: Optional[str]
    InvoiceItems: List[InvoiceItem]


class YardiResponse(BaseModel):
    vendor_file_name: str
    InvoiceStatus: str
    status: bool
    message: str
    etl_data: YardiEtlData


class InvoiceStatusResponse(BaseModel):
    InvoiceStatus: str
    status: bool
    message: str

# New schemas for the JSON Master Data payload from .NET
class MasterDataPayload(BaseModel):
    vendors: List[dict[str, Any]]
    properties: List[dict[str, Any]]
