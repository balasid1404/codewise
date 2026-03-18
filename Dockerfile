FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download ML models at build time (avoids slow runtime download)
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; SentenceTransformer('microsoft/codebert-base'); CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Copy source
COPY . .

# Expose port
EXPOSE 8080

# Run API
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
