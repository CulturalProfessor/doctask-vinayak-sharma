FROM python:3.12-slim

WORKDIR /app

# Dependencies first so a source edit does not invalidate the install layer.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e . 2>/dev/null || \
    pip install --no-cache-dir \
        "pydantic>=2.7" "psycopg[binary]>=3.1" "pyyaml>=6.0" "fastapi>=0.111" \
        "uvicorn[standard]>=0.30" "python-multipart>=0.0.9" "pypdf>=4.2" \
        "python-docx>=1.1" "beautifulsoup4>=4.12"

COPY app ./app
COPY config ./config
COPY corpora ./corpora
COPY recordings ./recordings

EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
