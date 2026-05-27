import requests
import json
import time
import base64
import logging
from app import config

def extract_invoice_data_from_memory(pdf_bytes: bytes) -> dict:
    logging.info("--- STARTING OCR SERVICE ---")
    base64_encoded_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
    data = {"base64Source": base64_encoded_pdf}
    
    headers = {'Content-Type': 'application/json', 'Ocp-Apim-Subscription-Key': config.AZURE_KEY}
    params = {'api-version': '2024-11-30', 'stringIndexType': 'textElements'}
    post_url = f'{config.AZURE_ENDPOINT}documentintelligence/documentModels/{config.AZURE_MODEL_NAME}:analyze'
    
    logging.info(f"Calling Azure Model: {config.AZURE_MODEL_NAME}")
    response = requests.post(post_url, headers=headers, params=params, data=json.dumps(data))
    if response.status_code != 202:
        logging.error(f"Azure API Error: {response.text}")
        raise Exception(f"Azure API Error: {response.text}")

    request_id = response.headers.get('apim-request-id')
    logging.info(f"Azure Request ID: {request_id}. Polling for results...")
    time.sleep(4)
    
    get_url = f'{config.AZURE_ENDPOINT}documentintelligence/documentModels/{config.AZURE_MODEL_NAME}/analyzeResults/{request_id}'
    
    while True:
        resp_get = requests.get(get_url, params={'api-version': '2024-11-30'}, headers=headers)
        result = resp_get.json()
        status = result.get("status")
        logging.info(f"Azure Status: {status}")
        
        if status == 'succeeded':
            break
        elif status == 'failed':
            logging.error("Azure processing failed.")
            raise Exception("Azure processing failed.")
        time.sleep(4)  

    return parse_azure_response(result)

def parse_azure_response(result: dict) -> dict:
    print("\n--- PARSING AZURE OCR JSON ---")
    docs = result.get("analyzeResult", {}).get("documents", [])
    if not docs: 
        print("❌ ERROR: Azure returned 0 documents.")
        return {}
        
    fields = docs[0].get("fields", {})
    print(f"Azure successfully found these fields: {list(fields.keys())}")
    
    def get_val(key):
        f = fields.get(key)
        if not f: return None
        return f.get("content") or f.get("valueString") or f.get("valueNumber") or f.get("valueDate")

    descriptions = []
    general_desc = get_val("description") or get_val("InvoiceNotes")
    if general_desc: descriptions.append(str(general_desc).replace('\n', ' '))
        
    for item in fields.get("Items", {}).get("valueArray", []):
        obj = item.get("valueObject", {})
        item_desc = obj.get("Description", {}).get("content") or obj.get("description", {}).get("content")
        if item_desc: descriptions.append(str(item_desc).replace('\n', ' '))
            
    dutch_description_string = " | ".join(descriptions)

    extracted = {
        "Invoice_Number": get_val("InvoiceId") or get_val("InvoiceNumber") or get_val("invoice_number"),
        "Vendor_Name": get_val("VendorName") or get_val("issuer"),
        "Property_Name": get_val("CustomerName") or get_val("CustomerAddress") or get_val("issue"), 
        "Invoice_Date": get_val("InvoiceDate") or get_val("date"),
        "Amount": float(get_val("InvoiceTotal") or get_val("amount") or 0.0),
        "Tax_Amount": float(get_val("TotalTax") or get_val("vat") or 0.0),
        "Description_Dutch": dutch_description_string
    }
    
    print("\n====== AZURE FINAL EXTRACTED DATA ======")
    print(extracted)
    print("========================================\n")
    
    return extracted