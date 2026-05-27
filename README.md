# Greystar Python AI Microservice
To run the server locally:
1. `pip install -r requirements.txt`
2. Run `uvicorn app.main:app --reload`
3. Access API Docs at: `http://127.0.0.1:8000/docs`
Note: The .NET team must upload the Excel files via the `/api/upload-expense-report` endpoint before processing invoices.
