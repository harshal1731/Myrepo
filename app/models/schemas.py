from pydantic import BaseModel
from typing import Dict, Any, List

class YardiResponse(BaseModel):
    vendor_file_name: str
    status: str
    etl_data: Dict[str, Any]

# New schemas for the JSON Master Data payload from .NET
class MasterDataPayload(BaseModel):
    vendors: List[Dict[str, Any]]
    properties: List[Dict[str, Any]]