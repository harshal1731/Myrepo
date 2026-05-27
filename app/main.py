import logging
import os
from fastapi import FastAPI
from app.api.routes import router
from app.config import LOGS_DIR, LOG_TO_FILE

# Setup Application-Wide Logging
handlers = [logging.StreamHandler()]
if LOG_TO_FILE:
    handlers.append(logging.FileHandler(os.path.join(LOGS_DIR, "api_errors.log")))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=handlers,
)

# Silence noisy background libraries
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)

app = FastAPI(
    title="Greystar Invoice AI API", 
    description="Microservice for Invoice OCR, Translation, and Yardi Mapping (In-Memory Version)",
    version="1.1"
)

# Include the endpoints from routes.py
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    # This allows you to run `python app/main.py` directly for testing
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
