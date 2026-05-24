FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
      "fastapi>=0.110" "uvicorn[standard]>=0.27" \
      "sqlalchemy>=2.0" "pydantic>=2.6" \
      "typer>=0.12" "rich>=13.7" \
      "networkx>=3.2" "pyyaml>=6.0"

# Now copy the source
COPY src ./src
COPY samples ./samples
COPY baselines.yaml ./
COPY README.md ./

RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Default: serve the API on 0.0.0.0:8000
CMD ["aegis", "serve", "--host", "0.0.0.0", "--port", "8000"]
