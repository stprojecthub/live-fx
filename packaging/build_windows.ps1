param(
    [string]$PythonExe = "",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "LIVE FX packaging requires 64-bit Windows."
}

if (-not $PythonExe) {
    $projectPython = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $projectPython) {
        $PythonExe = $projectPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python was not found. Install 64-bit Python or pass -PythonExe."
        }
        $PythonExe = $pythonCommand.Source
    }
}

if (-not (Test-Path $PythonExe)) {
    throw "Python interpreter not found: $PythonExe"
}

$version = (Get-Content (Join-Path $PSScriptRoot "VERSION") -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "packaging\VERSION must contain a semantic version such as 1.0.0."
}

$buildEnv = Join-Path $root ".build-venv"
$buildPython = Join-Path $buildEnv "Scripts\python.exe"
if (-not (Test-Path $buildPython)) {
    & $PythonExe -m venv $buildEnv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the isolated packaging environment."
    }
}

& $buildPython -m pip install -r (Join-Path $root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the application and packaging dependencies."
}

$iconPath = Join-Path $root "assets\live-fx.ico"
& $buildPython (Join-Path $PSScriptRoot "create_icon.py") --output $iconPath
if ($LASTEXITCODE -ne 0) {
    throw "Could not generate the application icon."
}

$portAudioDll = Join-Path $buildEnv "Lib\site-packages\_sounddevice_data\portaudio-binaries\libportaudio64bit.dll"
if (-not (Test-Path $portAudioDll)) {
    throw "The 64-bit PortAudio runtime was not found: $portAudioDll"
}

$versionResource = Join-Path $root "build\version_info.txt"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $versionResource) | Out-Null
$parts = $version.Split(".")
$versionTuple = "($($parts[0]), $($parts[1]), $($parts[2]), 0)"
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=$versionTuple, prodvers=$versionTuple, mask=0x3f,
                    flags=0x0, OS=0x4, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'LIVE FX'),
        StringStruct('FileDescription', 'LIVE FX - Stereo Space Processor'),
        StringStruct('FileVersion', '$version'),
        StringStruct('InternalName', 'Live FX'),
        StringStruct('OriginalFilename', 'Live FX.exe'),
        StringStruct('ProductName', 'LIVE FX'),
        StringStruct('ProductVersion', '$version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Set-Content -Path $versionResource -Value $versionInfo -Encoding ASCII

$dist = Join-Path $root "dist"
$work = Join-Path $root "build\pyinstaller"
& $buildPython -m PyInstaller --clean --noconfirm --onefile --windowed `
    --name "Live FX" --icon $iconPath --version-file $versionResource `
    --additional-hooks-dir (Join-Path $PSScriptRoot "hooks") `
    --distpath $dist --workpath $work --specpath (Join-Path $root "build") `
    (Join-Path $root "main.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build LIVE FX."
}

if ($SkipInstaller) {
    Write-Host "Application executable created: $(Join-Path $dist 'Live FX.exe')"
    exit 0
}

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $knownPaths = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($path in $knownPaths) {
        if ($path -and (Test-Path $path)) {
            $isccPath = $path
            break
        }
    }
} else {
    $isccPath = $iscc.Source
}

if (-not $isccPath) {
    throw "Inno Setup 6 was not found. Install it, then rerun this script to create the setup EXE."
}

$installerScript = Join-Path $PSScriptRoot "live-fx.iss"
$versionDefine = "/DMyAppVersion=`"$version`""
& $isccPath $versionDefine $installerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed to create the installer."
}

Write-Host "Installer created: $(Join-Path $dist "LIVE-FX-Setup-$version.exe")"
