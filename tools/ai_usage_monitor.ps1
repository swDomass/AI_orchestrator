param(
    [int]$SleepSeconds = 900,
    [switch]$Once
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$f = $null
try {
    $b64 = python -X utf8 -c "import config,base64; p=str(config.CAPACITY_LOG_FILE); print(base64.b64encode(p.encode('utf-8')).decode())"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($b64)) {
        throw "python could not resolve CAPACITY_LOG_FILE"
    }
    $f = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64.Trim()))
}
catch {
    # Keep monitor usable even if Python/config lookup fails.
    $f = ".\logs\capacity-log.md"
}

function Get-ProviderLines {
    param(
        [string]$Path,
        [string]$Provider
    )

    $rows = foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?<key>[^|]+) \| (?<pct>-?\d+(?:\.\d+)?) \| (?<avail>true|false)$") {
            $key = $matches["key"].Trim()
            if ($key -eq $Provider -or $key.StartsWith($Provider + "_")) {
                [PSCustomObject]@{
                    Ts    = [datetime]::ParseExact($matches["ts"], "yyyy-MM-dd HH:mm:ss", $null)
                    Key   = $key
                    Pct   = [double]$matches["pct"]
                    Avail = ($matches["avail"] -eq "true")
                }
            }
        }
    }

    if (-not $rows) {
        return @()
    }

    $latestTs = ($rows | Sort-Object Ts | Select-Object -Last 1).Ts
    return $rows | Where-Object { $_.Ts -eq $latestTs } | Sort-Object Key
}

function Write-ProviderSummary {
    param(
        [string]$Provider,
        [object[]]$Rows
    )

    if (-not $Rows -or $Rows.Count -eq 0) {
        Write-Host ("{0,-8}: keine Daten" -f $Provider) -ForegroundColor Yellow
        return
    }

    $remaining = switch ($Provider) {
        "gemini" { [math]::Round((($Rows | Measure-Object -Property Pct -Maximum).Maximum), 1) }
        default  { [math]::Round((($Rows | Measure-Object -Property Pct -Minimum).Minimum), 1) }
    }

    $ts = $Rows[0].Ts.ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host ("{0,-8}: {1,6:N1}% (snapshot {2})" -f $Provider, $remaining, $ts) -ForegroundColor Green
    foreach ($r in $Rows) {
        $window = if ($r.Key.StartsWith($Provider + "_")) { $r.Key.Substring($Provider.Length + 1) } else { "overall" }
        Write-Host ("  - {0,-28} {1,6:N1}%" -f $window, $r.Pct)
    }
}

while ($true) {
    Clear-Host
    Write-Host ("Letztes Update: " + (Get-Date -Format "HH:mm:ss")) -ForegroundColor Cyan

    if (Test-Path -LiteralPath $f) {
        $claudeRows = Get-ProviderLines -Path $f -Provider "claude"
        $geminiRows = Get-ProviderLines -Path $f -Provider "gemini"
        $codexRows = Get-ProviderLines -Path $f -Provider "codex"

        Write-Host ""
        Write-ProviderSummary -Provider "claude" -Rows $claudeRows
        Write-ProviderSummary -Provider "gemini" -Rows $geminiRows
        Write-ProviderSummary -Provider "codex" -Rows $codexRows
    }
    elseif (Test-Path -LiteralPath ".\\logs\\orchestrator.log") {
        Write-Host ("capacity-log fehlt: " + $f) -ForegroundColor Yellow
        Write-Host "Fallback: letzte check-limits aus logs/orchestrator.log" -ForegroundColor Yellow
        Select-String -Path ".\\logs\\orchestrator.log" -Pattern "Run check-limits|five_hour|seven_day|primary_window|secondary_window|gemini_" |
            Select-Object -Last 40 |
            ForEach-Object { $_.Line }
    }
    else {
        Write-Host "Weder capacity-log noch logs/orchestrator.log gefunden." -ForegroundColor Red
    }

    if ($Once) { break }
    Start-Sleep -Seconds $SleepSeconds
}
