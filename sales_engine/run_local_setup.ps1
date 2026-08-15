param(
  [string]$CsvPath = "C:\Nexcess\Path-Flow_ネイル対象店舗_113件.csv",
  [string]$LegacyDb = "C:\Nexcess\leads_database.db",
  [string]$DbPath = "$PSScriptRoot\sales_engine.db"
)

$ErrorActionPreference = "Stop"

Write-Host "[1/5] Install dependency"
python -m pip install -r "$PSScriptRoot\requirements.txt"

Write-Host "[2/5] Initialize campaign ledger"
python "$PSScriptRoot\init_ledger.py" "$CsvPath" --db "$DbPath"

Write-Host "[3/5] Import legacy contact data"
python "$PSScriptRoot\import_legacy_contacts.py" "$LegacyDb" --db "$DbPath"

Write-Host "[4/5] Enrich remaining contacts (dry data collection; no email send)"
python "$PSScriptRoot\contact_enrichment.py" --db "$DbPath"

Write-Host "[5/5] Campaign mail dry-run"
python "$PSScriptRoot\campaign_runner.py" --db "$DbPath"

Write-Host ""
Write-Host "===== DAILY REPORT ====="
python "$PSScriptRoot\daily_report.py" --db "$DbPath"

Write-Host ""
Write-Host "Dry-run completed. No email was sent."
