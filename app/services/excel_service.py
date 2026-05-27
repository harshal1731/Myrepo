import pandas as pd
import io
from thefuzz import process

# In-memory dataframes
df_master = pd.DataFrame()
df_vendor = pd.DataFrame()
df_expense = pd.DataFrame()

def load_master_data_from_json(payload: dict):
    global df_master, df_vendor
    try:
        df_vendor = pd.DataFrame(payload.get('vendors', []))
        if not df_vendor.empty:
            df_vendor.rename(columns={"RegisteredName": "PERSON", "TradingName": "Vendor name"}, inplace=True)
            
        df_master = pd.DataFrame(payload.get('properties', []))
        if not df_master.empty:
            df_master.rename(columns={"SiteCode": "Yardi code", "PropertyName1": "Property name"}, inplace=True)
            
        print(f"\n✅ SUCCESS: Loaded Master Data into RAM! Vendors: {len(df_vendor)} | Properties: {len(df_master)}")
    except Exception as e:
        print(f"\n❌ ERROR loading Master Data: {e}")
        raise e

def load_expense_report_from_memory(file_bytes: bytes):
    global df_expense
    try:
        df_expense = pd.read_excel(io.BytesIO(file_bytes), header=4).dropna(how='all')
        print(f"\n✅ SUCCESS: Loaded Expense Report into RAM! Rows: {len(df_expense)}")
    except Exception as e:
        print(f"\n❌ ERROR loading Expense Report: {e}")
        raise e

def get_vendor_person_code(raw_vendor_name: str) -> tuple:
    print(f"\n---> MATCHING VENDOR: Searching for '{raw_vendor_name}'")
    
    if df_vendor.empty:
        print("---> ❌ FAILED: Vendor DataFrame is EMPTY! Did you upload the JSON first?")
        return "NA", raw_vendor_name
        
    if not raw_vendor_name: 
        print("---> ❌ FAILED: Azure OCR did not find a Vendor Name on the invoice.")
        return "NA", raw_vendor_name
    
    vendors = df_vendor.get('Vendor name', pd.Series()).dropna().tolist()
    best_match, score = process.extractOne(raw_vendor_name, vendors)
    
    print(f"---> RESULT: Best match is '{best_match}' with a score of {score}%")
    
    if score >= 80:
        row = df_vendor[df_vendor['Vendor name'] == best_match].iloc[0]
        return str(row.get('PERSON', 'NA')), best_match
    return "NA", raw_vendor_name

def get_property_yardi_code(raw_property_name: str) -> str:
    print(f"\n---> MATCHING PROPERTY: Searching for '{raw_property_name}'")
    
    if df_master.empty:
        print("---> ❌ FAILED: Property DataFrame is EMPTY!")
        return "NA"
        
    if not raw_property_name: 
        print("---> ❌ FAILED: Azure OCR did not find a Property Name/Address on the invoice.")
        return "NA"
    
    properties = df_master.get('Property name', pd.Series()).dropna().tolist()
    best_match, score = process.extractOne(raw_property_name, properties)
    
    print(f"---> RESULT: Best match is '{best_match}' with a score of {score}%")
    
    if score >= 80:
        row = df_master[df_master['Property name'] == best_match].iloc[0]
        return str(row.get('Yardi code', 'NA'))
    return "NA"