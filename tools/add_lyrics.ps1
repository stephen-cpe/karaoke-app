<#
.SYNOPSIS
Generate lyrics for songs that do not have an SRT yet, then validate them.

.DESCRIPTION
Uses the cached Demucs/Whisper pipeline in tools/generate_lyrics.py.
Existing SRTs are never overwritten unless -Song is given explicitly.

.EXAMPLE
.\tools\add_lyrics.ps1

.EXAMPLE
.\tools\add_lyrics.ps1 -Song "Innocent"

.EXAMPLE
.\tools\add_lyrics.ps1 -Song "Innocent" -Force
#>
param(
    [string]$Song = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "tools\.cache\last_run.log"
$genArgs = @("tools\generate_lyrics.py", "--cache-dir", "tools\.cache")

if ($Song) {
    $genArgs += @("--song", $Song)
} else {
    $genArgs += "--missing"
}
if ($Force) {
    $genArgs += "--force"
}

Push-Location $root
try {
    Write-Host "Generating lyrics..." -ForegroundColor Cyan
    # Python logs to stderr. Redirect it without letting Stop turn normal log
    # lines into terminating NativeCommandError records.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & python @genArgs 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    $output = @($output | ForEach-Object { $_.ToString() })
    $output | Set-Content -Path $log -Encoding UTF8
    $output |
        Select-String -Pattern '===|reference:|wrote|done|failed|ERROR|WARNING|nothing to do|no matching' |
        ForEach-Object { Write-Host $_ }

    if ($code -ne 0) {
        Write-Host "`nGeneration failed. Last 40 log lines:" -ForegroundColor Red
        Get-Content $log -Tail 40
        exit $code
    }

    Write-Host "`nValidating..." -ForegroundColor Cyan
    $checkArgs = @("tools\check_lyrics.py", "--energy")
    if ($Song) {
        $checkArgs += @("--song", $Song)
    }
    $check = & python @checkArgs 2>&1
    $checkCode = $LASTEXITCODE
    $check | ForEach-Object { Write-Host $_ }
    exit $checkCode
} finally {
    Pop-Location
}
