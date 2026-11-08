# TUSKFiZZY installer
#
# Builds "TUSKFiZZY Setup <version>.exe" in dist-installer\: one file to give to someone else. It installs TUSKFiZZY
# Board for them with no administrator rights and no Python, Node.js or uv of their own, and adds it to their Start
# menu and desktop with its icon.
#
# Run it from this folder with:
#     powershell -ExecutionPolicy Bypass -File build-installer.ps1
#
# It freezes the core (the Python program) with PyInstaller in a Python environment of its own, .venv-package, so a Guild
# Board running from this checkout keeps running, and packs it into the Electron shell (shell\build\package.mjs). The first
# run installs Inno Setup, which makes the installer, for this Windows user with winget.
#
# A TUSKFiZZY installed from it takes an update only with a signature by the release key pinned in it (selfupdate.py).
# While none is pinned, the copy it installs could never update itself, so the script stops before it builds anything,
# unless it is run with -WithoutReleaseKey for an installer that never updates.

param([switch]$WithoutReleaseKey)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'tools\release-signing.ps1')

if (-not (Test-TrustedKey (Get-TrustedKey (Join-Path $PSScriptRoot 'src\guildboard\selfupdate.py')))) {
    if (-not $WithoutReleaseKey) {
        Write-Output ('No release key is pinned in src\guildboard\selfupdate.py, so a TUSKFiZZY installed from this ' +
            'installer could never update itself. Make the key once with tools\make-release-key.ps1 and pin the line it ' +
            'prints, or run this again with -WithoutReleaseKey for an installer that never updates.')
        exit 1
    }
    $keyWarning = 'No release key is pinned: a TUSKFiZZY installed from this installer will never update itself.'
    Write-Output $keyWarning
}

function Step([string]$What, [scriptblock]$Run) {
    Write-Output "- $What"
    $global:LASTEXITCODE = 0
    & $Run
    if ($LASTEXITCODE -ne 0) {
        Write-Output "This step did not work (exit code $LASTEXITCODE). The steps after it were not run."
        exit 1
    }
}

function Find-Iscc {
    $places = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    return $places | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Output "uv is not installed. Install it from https://docs.astral.sh/uv/ and run this script again."
    exit 1
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Output "Node.js is not installed. Install version 22 from https://nodejs.org/ and run this script again."
    exit 1
}

# A TUSKFiZZY that runs from this checkout runs from shell\app-dev\electron.exe, the copy of Electron's program that
# setup.ps1's shortcut starts, or from shell\node_modules\electron\dist\electron.exe (npm start), and the build replaces the
# interface and the shell it runs; npm ci below deletes the second. A program that runs cannot be opened for writing, so
# this opens each file for writing, sharing it with everyone (it writes nothing and opens no process); only an IOException,
# the file being in use, stops the build. It runs before anything is changed, and again just before npm ci of the shell: a
# TUSKFiZZY may start meanwhile.
function Assert-ShellNotRunning {
    foreach ($program in 'shell\app-dev\electron.exe', 'shell\node_modules\electron\dist\electron.exe') {
        $path = Join-Path $PSScriptRoot $program
        if (-not (Test-Path -LiteralPath $path)) { continue }
        try {
            [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]'ReadWrite, Delete').Dispose()
        } catch [System.IO.IOException] {
            Write-Output ("A TUSKFiZZY is running from this folder ($program is in use), and the build would replace " +
                'its files. Quit TUSKFiZZY first and run this again.')
            exit 1
        }
    }
}
Assert-ShellNotRunning

$version = (Select-String -LiteralPath pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$package = Join-Path $PSScriptRoot 'build\package'
$core = Join-Path $package 'dist\core'
$env:UV_PROJECT_ENVIRONMENT = '.venv-package'

if (-not (Find-Iscc)) {
    Step 'Install Inno Setup' {
        winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
    }
}
$iscc = Find-Iscc
if (-not $iscc) {
    Write-Output 'Inno Setup was installed, but ISCC.exe was not found. Run this script again in a new window.'
    exit 1
}

# Exactly the packages uv.lock names: a pyproject.toml that asks for others stops the build until uv lock is run.
Step 'Install the Python packages for the build' { uv sync --locked --no-dev --group package }
Step 'Generate the TUSKFiZZY app icon' { uv run --locked --no-dev --group package python tools/make-icon.py }
Step 'Install the packages of the interface' { npm ci --prefix web }
Step 'Build the interface' { npm run build --prefix web }
# The notices of the software inside the installer are a file of the repository, written by a script. A package that was added
# or changed since then stops the build here; run tools/third-party-notices.py, read the change and commit it.
Step 'Check the third-party notices' { uv run --locked --no-dev --group package python tools/third-party-notices.py --check }
New-Item -ItemType Directory -Force $package | Out-Null
$parts = @($version.Split('.') | ForEach-Object { [int]$_ }) + @(0, 0, 0)
$tuple = ($parts[0..3] -join ', ')
(Get-Content -LiteralPath packaging\version_info.txt -Raw).Replace('{tuple}', $tuple).Replace('{version}', $version) |
    Set-Content -LiteralPath (Join-Path $package 'version.txt') -Encoding utf8
Step 'Pack the core into one folder' {
    uv run --locked --no-dev --group package pyinstaller --noconfirm --clean `
        --distpath (Join-Path $package 'dist') --workpath (Join-Path $package 'work') packaging\guildboard.spec
}
if (-not (Test-Path -LiteralPath (Join-Path $core 'tuskfizzy-core.exe'))) {
    Write-Output "The core was not packed: $core holds no tuskfizzy-core.exe."
    exit 1
}
Assert-ShellNotRunning
Step 'Install the packages of the shell' { npm ci --prefix shell }
Step 'Test the shell' { npm test --prefix shell }
# npm ci can leave Electron's own program unfetched; it is fetched here when it is missing.
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'shell\node_modules\electron\dist\electron.exe'))) {
    Step 'Fetch the Electron program' { node (Join-Path $PSScriptRoot 'shell\node_modules\electron\install.js') }
}
Step 'Build the shell' { npm run build --prefix shell }
Step 'Pack the shell and the core' {
    Push-Location (Join-Path $PSScriptRoot 'shell')
    try { node build\package.mjs --core $core --version $version }
    finally { Pop-Location }
}
Step 'Make the installer' { & $iscc /Q "/DAppVersion=$version" packaging\installer.iss }

$setup = Join-Path $PSScriptRoot "dist-installer\TUSKFiZZY Setup $version.exe"
$size = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB)
Write-Output "The installer is ready: $setup ($size MB)."
if ($keyWarning) { Write-Output $keyWarning }
