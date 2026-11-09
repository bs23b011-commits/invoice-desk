# Starts the Invoice Desk API on http://localhost:8000
# Usage:  .\start-app.ps1
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Tesseract is installed but not on PATH, so the OCR reader needs this.
$env:Path += ";C:\Program Files\Tesseract-OCR"

$env:INTAKE_TOKEN     = "demo-token"
$env:PUBLIC_URL       = "http://localhost:8000"
$env:N8N_WEBHOOK_URL  = "http://localhost:5678/webhook/invoice-desk"

# Demo: your Gmail plays "Stores / receiving", so its replies are trusted like a colleague's.
$env:INTERNAL_EMAILS  = "your.name@gmail.com"

Write-Host "Invoice Desk -> http://localhost:8000" -ForegroundColor Green
python -m uvicorn app.main:app --port 8000
