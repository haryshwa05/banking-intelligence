$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Development only: source edits restart the API. Generated data and tests do
# not, so indexing and test runs cannot create a restart loop.
& .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload --reload-exclude "uploads/*" --reload-exclude "vector_store/*" --reload-exclude "rag_models/*" --reload-exclude "ocr_models/*" --reload-exclude "upload_metadata.db*" --reload-exclude "test_*.py"
