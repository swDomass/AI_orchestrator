# AI Orchestrator Crash-Resistant Wrapper
# Restarts the orchestrator automatically on crashes.
#
# Behaviour:
#   - Exit code 0           → graceful stop (Ctrl+C, /shutdown). Loop ends.
#   - Exit code != 0        → crash. Restart with exponential backoff.
#   - Crash loop (>=5 in 10 min) → wait 30 min, send Telegram alert, then keep trying.
#   - Each restart sends a Telegram notification (best-effort, ignored on failure).
#
# Crash log: logs/watchdog.log (rotated at 10 MB → watchdog.log.1)
#
# Compatible with both Windows PowerShell 5.1 and PowerShell 7+.
# Recommended start: pwsh -File run_orchestrator.ps1
#                    (or:  powershell.exe -File run_orchestrator.ps1)

$ErrorActionPreference = "Continue"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LogDir      = Join-Path $ScriptDir "logs"
$WatchdogLog = Join-Path $LogDir "watchdog.log"
$LogRotateMB = 10

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

# Rotate the watchdog log if it grew past the size cap. Keeps one .1 backup.
function Invoke-LogRotate {
    if (-not (Test-Path $WatchdogLog)) { return }
    try {
        $size = (Get-Item $WatchdogLog).Length
        if ($size -gt ($LogRotateMB * 1MB)) {
            $backup = "$WatchdogLog.1"
            if (Test-Path $backup) { Remove-Item $backup -Force -ErrorAction SilentlyContinue }
            Move-Item -Path $WatchdogLog -Destination $backup -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # Rotation is best-effort — never block startup on it
    }
}

function Write-WatchdogLog {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line      = "[$timestamp] [$Level] $Message"
    Add-Content -Path $WatchdogLog -Value $line -Encoding utf8
    $color = switch ($Level) {
        "ERROR" { "Red" }
        "WARN"  { "Yellow" }
        "OK"    { "Green" }
        default { "Cyan" }
    }
    Write-Host $line -ForegroundColor $color
}

# Strip a leading UTF-8 BOM (﻿) from the first line of a file. PS 5.1's
# Get-Content -Encoding utf8 does NOT strip the BOM; PS 7+ does. Either way
# this keeps the parser robust.
function Remove-BomPrefix {
    param([string]$Text)
    if ($null -eq $Text) { return $Text }
    if ($Text.Length -gt 0 -and [int][char]$Text[0] -eq 0xFEFF) {
        return $Text.Substring(1)
    }
    return $Text
}

function Get-EnvValue {
    param([string]$Key, [string]$EnvFile)
    if (-not (Test-Path $EnvFile)) { return $null }
    $isFirstLine = $true
    foreach ($line in Get-Content $EnvFile -Encoding utf8) {
        if ($isFirstLine) {
            $line = Remove-BomPrefix $line
            $isFirstLine = $false
        }
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed -split '=', 2
        if ($parts.Count -eq 2 -and $parts[0].Trim() -eq $Key) {
            $value = $parts[1].Trim()
            # Strip surrounding quotes (and any trailing comment after them)
            if ($value -match '^"([^"]*)"\s*(#.*)?$') {
                return $Matches[1]
            }
            if ($value -match "^'([^']*)'\s*(#.*)?$") {
                return $Matches[1]
            }
            # Unquoted: strip trailing inline comment ONLY when whitespace
            # precedes the '#' (matches Python's _normalize_dotenv_value rule
            # in config.py — protects URLs/hashes that legitimately contain '#').
            if ($value -match '^(.*?)\s+#') {
                $value = $Matches[1].Trim()
            }
            return $value
        }
    }
    return $null
}

function Send-TelegramAlert {
    param([string]$Text)
    try {
        $envFile = Join-Path $ScriptDir ".env"
        $token   = Get-EnvValue -Key "TELEGRAM_BOT_TOKEN" -EnvFile $envFile
        $chatId  = Get-EnvValue -Key "TELEGRAM_CHAT_ID"   -EnvFile $envFile
        if (-not $token -or -not $chatId) { return }

        # Defensive: drop any control chars sneaked in via .env (CR/LF would
        # split the URL path). Then URL-encode the token.
        $token   = ($token -replace '[\r\n\t]', '')
        $tokenEnc = [uri]::EscapeDataString($token)
        $url      = "https://api.telegram.org/bot$tokenEnc/sendMessage"
        $body     = @{ chat_id = $chatId; text = $Text }
        Invoke-RestMethod -Uri $url -Method Post -Body $body -TimeoutSec 10 | Out-Null
    } catch {
        # Telegram is optional; never block the watchdog on notification failure
    }
}

# Crash-loop tracking: list of crash timestamps (datetime), trimmed to last 10 min
$crashWindow = New-Object System.Collections.Generic.List[datetime]
$crashLoopThreshold     = 5
$crashLoopWindowMinutes = 10
$baseBackoffSec         = 10
$maxBackoffSec          = 300       # 5 min between normal-crash restarts
$crashLoopBackoffSec    = 1800      # 30 min after a crash storm

Invoke-LogRotate
Write-WatchdogLog "Watchdog gestartet (Skript: $ScriptDir)" "OK"

while ($true) {
    Invoke-LogRotate
    Write-WatchdogLog "Starte AI Orchestrator (--watch)..." "INFO"
    $startedAt = Get-Date

    # -X utf8: ensure stdout uses UTF-8 on Windows for emoji/umlauts
    & python -X utf8 (Join-Path $ScriptDir "orchestrator.py") --watch
    $exitCode = $LASTEXITCODE

    $duration = (Get-Date) - $startedAt
    $durStr   = "{0:hh\:mm\:ss}" -f $duration

    if ($exitCode -eq 0) {
        Write-WatchdogLog "Orchestrator regulär beendet (Laufzeit $durStr)." "OK"
        break
    }

    Write-WatchdogLog "Orchestrator abgestürzt (Exit $exitCode, Laufzeit $durStr)." "ERROR"

    # Track crash within the rolling window. Where-Object filter works on
    # both Windows PowerShell 5.1 and PowerShell 7+ (unlike the
    # [System.Predicate[datetime]] ScriptBlock cast that fails on 5.1).
    $now = Get-Date
    $crashWindow.Add($now) | Out-Null
    $cutoff = $now.AddMinutes(-$crashLoopWindowMinutes)
    $kept = @($crashWindow | Where-Object { $_ -ge $cutoff })
    $crashWindow.Clear()
    foreach ($ts in $kept) { $crashWindow.Add($ts) | Out-Null }

    if ($crashWindow.Count -ge $crashLoopThreshold) {
        $msg = "🚨 Orchestrator-Crash-Schleife: $($crashWindow.Count) Abstürze in $crashLoopWindowMinutes min. Warte $($crashLoopBackoffSec / 60) min bis zum nächsten Versuch."
        Write-WatchdogLog $msg "ERROR"
        Send-TelegramAlert $msg
        Start-Sleep -Seconds $crashLoopBackoffSec
        $crashWindow.Clear()
        continue
    }

    # Exponential backoff capped by maxBackoffSec
    $backoff = [Math]::Min($maxBackoffSec, $baseBackoffSec * [Math]::Pow(2, ($crashWindow.Count - 1)))
    $msg = "⚠️ Orchestrator gecrasht (Exit $exitCode). Neustart in ${backoff}s. ($($crashWindow.Count) Crash(s) in den letzten $crashLoopWindowMinutes min)"
    Write-WatchdogLog "Backoff ${backoff}s vor Restart." "WARN"
    Send-TelegramAlert $msg

    Start-Sleep -Seconds $backoff
}

Write-WatchdogLog "Watchdog beendet." "OK"
