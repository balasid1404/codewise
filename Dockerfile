FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the ML model at build time (avoids slow runtime download)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('microsoft/codebert-base')"

# Copy source
COPY . .

# Expose port
EXPOSE 8080

# Run API
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
