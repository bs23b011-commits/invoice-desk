# Starts n8n on http://localhost:5678
# Usage:  .\start-n8n.ps1
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Read by the workflows' HTTP Request nodes via {{$env...}}
$env:INVOICE_DESK_URL   = "http://127.0.0.1:8000"   # not localhost: Node picks IPv6 ::1, the app listens on IPv4
$env:INVOICE_DESK_TOKEN = "demo-token"

# n8n blocks $env access inside nodes by default; the workflows rely on it.
$env:N8N_BLOCK_ENV_ACCESS_IN_NODE = "false"

Write-Host "n8n -> http://localhost:5678" -ForegroundColor Green
npx -y n8n@1.123.92
