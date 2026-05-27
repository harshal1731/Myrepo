# 1. Use the official, lightweight Python 3.10 image
FROM python:3.10-slim

# 2. Set environment variables to make Python run smoother in Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# 3. Set the working directory inside the container
WORKDIR /app

# 4. Install system dependencies required by Pandas, C-extensions, and AI tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 5. Copy the requirements file FIRST (this helps Docker cache the installation step)
COPY requirements.txt .

# 6. Install the Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 7. Copy the rest of your application code into the container
COPY . .

# 8. Expose the port that Uvicorn will run on
EXPOSE 8000

# 9. The command that boots up your API when the container starts
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips '*'"]
