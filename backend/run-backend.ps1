$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Normal operation: no file watcher. Models are loaded once at startup and
# remain in memory until this process is deliberately stopped.
& .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
