# Builds the Windows release:  Release\VideoDownloader_Setup.exe, Release\VideoDownloader_Portable.exe
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Requirements on the BUILD machine only: Python 3.10+ and Inno Setup 6
# (winget install JRSoftware.InnoSetup). End users need nothing.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1. Python environment -------------------------------------------------------
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q --upgrade pip
.\.venv\Scripts\pip install -q -r requirements-build.txt

# 2. Bundled tools: FFmpeg (yt-dlp build) + Deno, verified by SHA-256 ----------
function Get-Verified($url, $shaUrl, $out, $match) {
    if (Test-Path $out) { return }
    Write-Host "Downloading $url"
    Invoke-WebRequest $url -OutFile $out -UseBasicParsing
    $sums = (Invoke-WebRequest $shaUrl -UseBasicParsing).Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
    $actual = (Get-FileHash $out -Algorithm SHA256).Hash.ToLower()
    if (-not ($sums.ToLower().Contains($actual))) { Remove-Item $out; throw "Checksum mismatch for $out" }
    Write-Host "  checksum OK ($actual)"
}
New-Item -ItemType Directory -Force vendor\dl, vendor\bin, vendor\licenses | Out-Null
Get-Verified "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip" `
             "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256" `
             "vendor\dl\ffmpeg.zip" "win64-gpl-shared.zip"
Get-Verified "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" `
             "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip.sha256sum" `
             "vendor\dl\deno.zip" "deno"
if (-not (Test-Path vendor\bin\ffmpeg.exe)) {
    Remove-Item -Recurse -Force vendor\x -ErrorAction SilentlyContinue
    Expand-Archive vendor\dl\ffmpeg.zip vendor\x\ff
    Expand-Archive vendor\dl\deno.zip vendor\x\deno
    $ff = Get-ChildItem vendor\x\ff -Directory | Select-Object -First 1
    Copy-Item "$($ff.FullName)\bin\ffmpeg.exe", "$($ff.FullName)\bin\ffprobe.exe", "$($ff.FullName)\bin\*.dll" vendor\bin\
    Copy-Item "$($ff.FullName)\LICENSE.txt" vendor\licenses\FFmpeg-LICENSE.txt
    Copy-Item vendor\x\deno\deno.exe vendor\bin\
}

# 3. Icons + PyInstaller ------------------------------------------------------
.\.venv\Scripts\python packaging\make_icon.py
Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
.\.venv\Scripts\pyinstaller packaging\VideoDownloader.spec --noconfirm --clean --distpath dist --workpath build
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# 4. Installer ----------------------------------------------------------------
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup" }
New-Item -ItemType Directory -Force Release | Out-Null
& $iscc /Q packaging\installer.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }

# 5. Release folder -----------------------------------------------------------
Copy-Item dist\VideoDownloader_Portable.exe Release\ -Force
Copy-Item packaging\RELEASE_README.md Release\README.md -Force
Remove-Item -Recurse -Force Release\source -ErrorAction SilentlyContinue
New-Item -ItemType Directory Release\source | Out-Null
Copy-Item app.py, requirements.txt, requirements-build.txt, build.ps1, README.md Release\source\
Copy-Item static, packaging, tests Release\source\ -Recurse
Get-ChildItem Release\source -Recurse -Include __pycache__ -Directory | Remove-Item -Recurse -Force

Get-ChildItem Release -File | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
Write-Host "`nDone. See the Release folder."
