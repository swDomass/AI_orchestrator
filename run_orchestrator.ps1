# AI Orchestrator Crash-Resistant Wrapper
# Restarts the orchestrator automatically in case of crashes.

while ($true) {
    Write-Host "`n[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Starte AI Orchestrator (--watch)..." -ForegroundColor Cyan
    
    # Run the orchestrator
    # We use -X utf8 for correct character handling on Windows
    python -X utf8 orchestrator.py --watch
    
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Orchestrator regulär beendet." -ForegroundColor Green
        break
    } else {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Orchestrator mit Fehler beendet (Exit Code: $exitCode). Neustart in 10s..." -ForegroundColor Red
        Start-Sleep -Seconds 10
    }
}
