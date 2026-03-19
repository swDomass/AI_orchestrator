# AI Orchestrator - WezTerm Telegram Notifier
# Mimics WezTerm's "long running process" notification but sends to Telegram.

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public class Win32 {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
}
"@ -ErrorAction SilentlyContinue

function Get-ForegroundProcessId {
    $hwnd = [Win32]::GetForegroundWindow()
    if ($hwnd -eq [IntPtr]::Zero) { return 0 }
    $processId = 0
    [Win32]::GetWindowThreadProcessId($hwnd, [ref]$processId) | Out-Null
    return $processId
}

function Is-MyWindowFocused {
    $fgId = Get-ForegroundProcessId
    if ($fgId -eq 0) { return $false }
    
    $currentId = $PID
    # Check if current process or any parent matches the foreground process
    # This works because WezTerm is usually a parent of the PowerShell process.
    while ($currentId -ne 0) {
        if ($currentId -eq $fgId) { return $true }
        try {
            # Compatible with both PowerShell 5.1 and 7+
            $currentId = (Get-CimInstance Win32_Process -Filter "ProcessId = $currentId").ParentProcessId
            if (-not $currentId) { $currentId = 0 }
        } catch {
            $currentId = 0
        }
    }
    return $false
}

function t_notify {
    param(
        [string]$Command,
        [Parameter(ValueFromRemainingArguments=$true)]
        $Arguments
    )

    $start = Get-Date
    # Run the actual command
    & $Command @Arguments
    $end = Get-Date
    
    $duration = ($end - $start).TotalSeconds
    
    # Threshold: 5 seconds (standard for WezTerm/Zsh/Fish notifications)
    if ($duration -gt 5) {
        if (-not (Is-MyWindowFocused)) {
            $argString = $Arguments -join " "
            $msg = "🔔 *Task Finished*`n`n`$ $Command $argString`n⏱ Dauer: $([Math]::Round($duration, 1))s"
            
            # Call your Orchestrator's notifier (path relative to this script)
            python "$PSScriptRoot\..\notifier.py" "$msg"
        }
    }
}

# Bot Aliases - Add more here if needed
function claude { t_notify "claude" @args }
function gemini { t_notify "gemini" @args }
function codex  { t_notify "codex"  @args }
