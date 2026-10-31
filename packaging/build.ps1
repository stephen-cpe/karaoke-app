# Build the portable Windows x64 KARAOKE APP distribution.
#
# This is the single packaging implementation used both locally and by
# GitHub Actions.  It produces:
#   dist/karaoke-app/                 extracted, runnable folder
#   dist/karaoke-app-v<version>-win64.zip
#   dist/karaoke-app-v<version>-win64.zip.sha256
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Python = "python",
    [switch]$SkipTests,
    [switch]$SkipSelfTest,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $PSBoundParameters.ContainsKey("Python") -and
        (Test-Path (Join-Path $Root ".venv-build\Scripts\python.exe"))) {
    $Python = Join-Path $Root ".venv-build\Scripts\python.exe"
}
$PythonExe = (Get-Command $Python -ErrorAction Stop).Source
& $PythonExe -c "import pystray, PIL, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python environment lacks desktop/build dependencies. Install requirements-desktop.txt and requirements-build.txt first."
}
$SourceVersion = (& $PythonExe -c "from karaoke.version import __version__; print(__version__)").Trim()
if (-not $Version) {
    $Version = $SourceVersion
}
$Version = $Version.Trim() -replace '^[vV]', ''
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Invalid version '$Version'. Use a value such as 0.0.1 or v0.0.1."
}
if ($Version -ne $SourceVersion) {
    throw "Version '$Version' does not match karaoke/version.py ('$SourceVersion')."
}

$Tag = "v$Version"
$BuildDir = Join-Path $Root "packaging\build"
$WorkDir = Join-Path $Root "build\pyinstaller-work"
$PyInstallerDist = Join-Path $Root "build\pyinstaller-dist"
$StageDir = Join-Path $Root "dist\karaoke-app"
$ZipPath = Join-Path $Root "dist\karaoke-app-$Tag-win64.zip"

Write-Host "KARAOKE APP $Tag build" -ForegroundColor Cyan
Write-Host "Python: $PythonExe"
Write-Host "Root:   $Root"

if (-not $SkipTests) {
    Write-Host "`nRunning Python tests..." -ForegroundColor Cyan
    & $PythonExe -m pytest -m "not browser"
    if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }

    $Node = Get-Command node -ErrorAction SilentlyContinue
    if ($Node) {
        Write-Host "`nRunning JavaScript tests..." -ForegroundColor Cyan
        & $Node.Source --test tests\js\reconcile.test.js
        if ($LASTEXITCODE -ne 0) { throw "JavaScript tests failed." }
    } else {
        Write-Warning "Node.js is not installed; skipping JavaScript tests."
    }

    Write-Host "`nRunning Ruff..." -ForegroundColor Cyan
    & $PythonExe -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed." }
}

if (Test-Path $BuildDir) {
    Remove-Item -Recurse -Force $BuildDir
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

Write-Host "`nGenerating icon and version resource..." -ForegroundColor Cyan
& $PythonExe packaging\build_assets.py --version $Version
if ($LASTEXITCODE -ne 0) { throw "Build asset generation failed." }

# --- FFmpeg -----------------------------------------------------------------
$FfmpegVersion = "9.0.2"
$FfmpegUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVersion/ffmpeg-$FfmpegVersion-essentials_build.zip"
$FfmpegSha = "60F467265B1E312373DBCD92200C2618A74850F98D3D078E94296BB3FA2047BA"
$CacheDir = Join-Path $Root "packaging\cache"
$FfmpegZip = Join-Path $CacheDir "ffmpeg-$FfmpegVersion-essentials_build.zip"
$FfmpegExtract = Join-Path $CacheDir "ffmpeg-$FfmpegVersion"
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

if (-not (Test-Path $FfmpegZip)) {
    Write-Host "`nDownloading FFmpeg $FfmpegVersion..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $FfmpegUrl -OutFile $FfmpegZip
}
$ActualSha = (Get-FileHash -Algorithm SHA256 $FfmpegZip).Hash
if ($ActualSha -ne $FfmpegSha) {
    throw "FFmpeg checksum mismatch: expected $FfmpegSha, got $ActualSha"
}
if (-not (Test-Path $FfmpegExtract)) {
    Write-Host "Extracting FFmpeg..." -ForegroundColor Cyan
    Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegExtract
}
$FfmpegExe = Get-ChildItem -Path $FfmpegExtract -Recurse -Filter ffmpeg.exe |
    Select-Object -First 1
if (-not $FfmpegExe) { throw "ffmpeg.exe was not found in the FFmpeg archive." }
$FfmpegPackage = Split-Path -Parent (Split-Path -Parent $FfmpegExe.FullName)
$FfmpegLicense = Join-Path $FfmpegPackage "LICENSE"
$FfmpegReadme = Join-Path $FfmpegPackage "README.txt"

$FfmpegBuildDir = Join-Path $BuildDir "ffmpeg"
New-Item -ItemType Directory -Force -Path $FfmpegBuildDir | Out-Null
Copy-Item $FfmpegExe.FullName (Join-Path $FfmpegBuildDir "ffmpeg.exe")
if (Test-Path $FfmpegLicense) { Copy-Item $FfmpegLicense (Join-Path $FfmpegBuildDir "LICENSE") }
if (Test-Path $FfmpegReadme) { Copy-Item $FfmpegReadme (Join-Path $FfmpegBuildDir "README.txt") }
& $FfmpegExe.FullName -hide_banner -version |
    Set-Content -Path (Join-Path $BuildDir "FFMPEG-VERSION.txt") -Encoding UTF8
$FilterOutput = & $FfmpegExe.FullName -hide_banner -filters 2>$null
if (-not ($FilterOutput | Select-String -Pattern '\brubberband\b')) {
    throw "The bundled FFmpeg does not advertise the required rubberband filter."
}

# --- PyInstaller -------------------------------------------------------------
Write-Host "`nBuilding PyInstaller bundle..." -ForegroundColor Cyan
if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
if (Test-Path $PyInstallerDist) { Remove-Item -Recurse -Force $PyInstallerDist }
& $PythonExe -m PyInstaller --noconfirm --clean `
    --distpath $PyInstallerDist --workpath $WorkDir `
    packaging\karaoke-app.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$BuiltDir = Join-Path $PyInstallerDist "karaoke-app"
if (-not (Test-Path $BuiltDir)) { throw "PyInstaller output folder was not created." }

# --- Stage distribution ------------------------------------------------------
Write-Host "`nStaging distribution..." -ForegroundColor Cyan
if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
Copy-Item -Recurse $BuiltDir $StageDir
foreach ($Folder in @("songs", "lyrics", "transposed")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $StageDir $Folder) | Out-Null
}
Copy-Item LICENSE $StageDir
Copy-Item THIRD_PARTY_NOTICES.md $StageDir
$DistributionReadme = Get-Content packaging\README-DISTRIBUTION.txt -Raw
$DistributionReadme = $DistributionReadme.Replace("{{VERSION}}", "$Version (PRE-ALPHA)")
Set-Content -Path (Join-Path $StageDir "README-DISTRIBUTION.txt") `
    -Value $DistributionReadme -Encoding UTF8
Copy-Item docs\VERSIONING.md $StageDir\VERSIONING.md
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "ffmpeg") | Out-Null
Copy-Item packaging\FFMPEG-SOURCE-OFFER.txt (Join-Path $StageDir "ffmpeg")
if (Test-Path (Join-Path $BuildDir "ffmpeg")) {
    Copy-Item (Join-Path $BuildDir "ffmpeg\*") (Join-Path $StageDir "ffmpeg") -Recurse
}
Copy-Item (Join-Path $BuildDir "karaoke-app.ico") (Join-Path $StageDir "_internal\static\karaoke-app.ico")
& $PythonExe packaging\copy_licenses.py (Join-Path $StageDir "licenses")
if ($LASTEXITCODE -ne 0) { throw "License staging failed." }

# --- Verification ------------------------------------------------------------
Write-Host "`nVerifying distribution..." -ForegroundColor Cyan
$required = @(
    (Join-Path $StageDir "karaoke-app.exe"),
    (Join-Path $StageDir "songs"),
    (Join-Path $StageDir "lyrics"),
    (Join-Path $StageDir "transposed"),
    (Join-Path $StageDir "ffmpeg\ffmpeg.exe"),
    (Join-Path $StageDir "_internal\templates\index.html"),
    (Join-Path $StageDir "_internal\templates\remote.html"),
    (Join-Path $StageDir "_internal\static\scripts.js"),
    (Join-Path $StageDir "_internal\static\remote.js"),
    (Join-Path $StageDir "_internal\static\sync_editor.js"),
    (Join-Path $StageDir "_internal\static\vendor\wavesurfer.min.js"),
    (Join-Path $StageDir "_internal\static\manifest.webmanifest")
)
foreach ($Path in $required) {
    if (-not (Test-Path $Path)) { throw "Required distribution item missing: $Path" }
}

$ForbiddenExtensions = @(
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
    ".aiff", ".mp4", ".webm", ".mov", ".m4v", ".ogv", ".srt"
)
$ForbiddenFiles = Get-ChildItem -Path $StageDir -Recurse -File | Where-Object {
    $ForbiddenExtensions -contains $_.Extension.ToLowerInvariant() -or
    $_.Name.ToLowerInvariant().EndsWith(".words.json")
}
if ($ForbiddenFiles) {
    $Names = ($ForbiddenFiles | Select-Object -ExpandProperty FullName) -join "`n"
    throw "Forbidden media or lyric files found in distribution:`n$Names"
}
$ForbiddenNames = @(
    "tools", "tests", "venv", ".venv-build", "__pycache__", "torch",
    "demucs", "faster_whisper", "numpy", "nvidia", "ctranslate2",
    "playwright", "av", "onnxruntime", "jedi", "matplotlib", "IPython"
)
$ForbiddenDirs = @()
foreach ($Name in $ForbiddenNames) {
    foreach ($Root in @($StageDir, (Join-Path $StageDir "_internal"))) {
        $Candidate = Join-Path $Root $Name
        if (Test-Path $Candidate) { $ForbiddenDirs += $Candidate }
    }
}
if ($ForbiddenDirs) {
    throw "Forbidden development/AI directories found in distribution:`n$($ForbiddenDirs -join "`n")"
}

$ExeInfo = (Get-Item (Join-Path $StageDir "karaoke-app.exe")).VersionInfo
if ($ExeInfo.FileVersion -notlike "$Version.0*") {
    throw "Executable FileVersion '$($ExeInfo.FileVersion)' does not match $Version.0"
}

$ExeSize = (Get-Item (Join-Path $StageDir "karaoke-app.exe")).Length
Write-Host ("Executable: {0:N0} bytes" -f $ExeSize)

if (-not $SkipSelfTest) {
    Write-Host "`nRunning packaged self-test..." -ForegroundColor Cyan
    $SelfTest = Start-Process -FilePath (Join-Path $StageDir "karaoke-app.exe") `
        -ArgumentList "--self-test" -Wait -PassThru
    if ($SelfTest.ExitCode -ne 0) {
        throw "Packaged self-test failed with exit code $($SelfTest.ExitCode)."
    }
    $SelfTestLog = Join-Path $StageDir "karaoke.log"
    if (Test-Path $SelfTestLog) { Remove-Item -Force $SelfTestLog }
}

if (-not $NoZip) {
    Write-Host "`nCreating ZIP..." -ForegroundColor Cyan
    if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
    Compress-Archive -Path $StageDir -DestinationPath $ZipPath -CompressionLevel Optimal
    $ZipHash = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLowerInvariant()
    "$ZipHash  $(Split-Path -Leaf $ZipPath)" |
        Set-Content -Path "$ZipPath.sha256" -Encoding ASCII
    $ZipSize = (Get-Item $ZipPath).Length
    Write-Host ("ZIP:        {0:N0} bytes" -f $ZipSize)
    Write-Host ("SHA-256:    {0}" -f $ZipHash)
}

Write-Host "`nBuild complete: $Tag" -ForegroundColor Green
Write-Host "Distribution: $StageDir"
if (-not $NoZip) { Write-Host "Archive:      $ZipPath" }
