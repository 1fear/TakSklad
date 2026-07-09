[CmdletBinding()]
param(
    [string]$OutputDir = "outputs\windows_test_build",
    [string]$BuildDir = "build\windows_test",
    [string]$Python = "python",
    [string]$MinAppVersion = "2.0.25",
    [string]$ExpectedBuildLabel = "MVP 2.0",
    [switch]$InstallDependencies,
    [switch]$SkipTests,
    [switch]$SkipVersionJsonGitCheck,
    [switch]$AllowUpdatedVersionManifest,
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"

function Show-TakSkladBuildHelp {
    Write-Host "TakSklad Windows test archive builder"
    Write-Host ""
    Write-Host "Purpose:"
    Write-Host "  Build a local Windows acceptance archive."
    Write-Host "  Public version.json and GitHub Release are handled by the release workflow."
    Write-Host ""
    Write-Host "Examples:"
    Write-Host '  .\tools\build_windows_test_archive.ps1'
    Write-Host '  .\tools\build_windows_test_archive.ps1 -InstallDependencies'
    Write-Host '  .\tools\build_windows_test_archive.ps1 -SkipTests'
}

function Get-ProjectRoot {
    return (Split-Path -Path $PSScriptRoot -Parent)
}

function Invoke-CheckedCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Host "> $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Get-TakSkladAppVersion {
    param([string]$ProjectRoot)

    $ConfigPath = Join-Path $ProjectRoot "src\taksklad\config.py"
    if (-not (Test-Path $ConfigPath)) {
        throw "config.py not found: $ConfigPath"
    }

    $Text = Get-Content $ConfigPath -Raw
    if ($Text -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') {
        throw "APP_VERSION not found in config.py"
    }

    return $Matches[1]
}

function Get-TakSkladBuildLabel {
    param([string]$ProjectRoot)

    $ConfigPath = Join-Path $ProjectRoot "src\taksklad\config.py"
    if (-not (Test-Path $ConfigPath)) {
        throw "config.py not found: $ConfigPath"
    }

    $Text = Get-Content $ConfigPath -Raw
    if ($Text -match 'APP_BUILD_LABEL\s*=\s*os\.environ\.get\("TAKSKLAD_BUILD_LABEL",\s*"([^"]*)"\)') {
        return $Matches[1]
    }
    if ($Text -match 'APP_BUILD_LABEL\s*=\s*"([^"]*)"') {
        return $Matches[1]
    }

    return ""
}

function Convert-VersionParts {
    param([string]$Version)

    $Numbers = [regex]::Matches($Version, "\d+") | ForEach-Object { [int]$_.Value }
    if (-not $Numbers -or $Numbers.Count -eq 0) {
        return @(0)
    }
    return @($Numbers | Select-Object -First 4)
}

function Compare-TakSkladVersion {
    param(
        [string]$Left,
        [string]$Right
    )

    $LeftParts = Convert-VersionParts $Left
    $RightParts = Convert-VersionParts $Right
    $Max = [Math]::Max($LeftParts.Count, $RightParts.Count)

    for ($Index = 0; $Index -lt $Max; $Index++) {
        $LeftValue = if ($Index -lt $LeftParts.Count) { $LeftParts[$Index] } else { 0 }
        $RightValue = if ($Index -lt $RightParts.Count) { $RightParts[$Index] } else { 0 }
        if ($LeftValue -lt $RightValue) { return -1 }
        if ($LeftValue -gt $RightValue) { return 1 }
    }

    return 0
}

function Assert-VersionJsonSafeForTestBuild {
    param([string]$ProjectRoot)

    $VersionJsonPath = Join-Path $ProjectRoot "version.json"
    if (-not (Test-Path $VersionJsonPath)) {
        throw "version.json not found: $VersionJsonPath"
    }

    if (-not $SkipVersionJsonGitCheck) {
        $Git = Get-Command git -ErrorAction SilentlyContinue
        if ($Git) {
            & $Git.Source diff --quiet -- version.json
            if ($LASTEXITCODE -ne 0) {
                throw "version.json has local changes. Test archive build must not change public update manifest."
            }
        }
    }

    if (-not $AllowUpdatedVersionManifest) {
        $Manifest = Get-Content $VersionJsonPath -Raw | ConvertFrom-Json
        $IsStablePinned = (
            $Manifest.latest_version -eq "1.1.7" -and
            $Manifest.min_supported_version -eq "1.1.7" -and
            -not $Manifest.download_url -and
            -not $Manifest.download_url_onedir
        )
        $IsSafeRollout = (
            $Manifest.latest_version -eq $MinAppVersion -and
            $Manifest.min_supported_version -eq $MinAppVersion -and
            $Manifest.mandatory -eq $true -and
            $Manifest.download_url -and
            $Manifest.sha256 -and
            $Manifest.download_url_onedir -and
            $Manifest.sha256_onedir
        )
        if (-not $IsStablePinned -and -not $IsSafeRollout) {
            throw "Public version.json is neither paused 1.1.7 nor forced 2.0.25 rollout manifest."
        }
    }
}

function Write-TestBuildReadme {
    param(
        [string]$Path,
        [string]$AppVersion,
        [string]$AppBuildLabel,
        [string]$PackageName
    )

    $Text = @"
# TakSklad Windows Test Build

Package: $PackageName
App version: $AppVersion
Build label: $AppBuildLabel

This archive is for Windows acceptance only.

Rules:
- Do not upload this archive to GitHub Release.
- Do not update public version.json with this archive.
- Do not use the old desktop shortcut for acceptance.
- Run acceptance through tools\windows_backend_acceptance.ps1.
- Keep service tokens outside this folder.

Recommended launch:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad\TakSklad.exe"
```

Acceptance files are in the acceptance folder.
"@

    $Text | Out-File -FilePath $Path -Encoding utf8
}

function Assert-TestPackageDoesNotContainLocalSecrets {
    param([string]$PackageRoot)

    $ForbiddenNames = @(
        "TakSklad_data.json",
        "TakSklad_data.json.last_good.*.bak",
        "TakSklad_data.json.*.tmp",
        "credentials.json",
        "telegram_settings.json",
        "yandex_geocoder_key.txt",
        "pending_saves.json",
        "pending_prints.json",
        "pending_telegram.json",
        "pending_backend_events.json",
        "*.log"
    )

    foreach ($Name in $ForbiddenNames) {
        $Matches = Get-ChildItem -Path $PackageRoot -Recurse -Force -File -Filter $Name -ErrorAction SilentlyContinue
        if ($Matches) {
            throw "Test package contains local runtime/secret file: $($Matches[0].FullName)"
        }
    }
}

if ($ShowHelp) {
    Show-TakSkladBuildHelp
    exit 0
}

$ProjectRoot = Get-ProjectRoot
Set-Location $ProjectRoot

$AppVersion = Get-TakSkladAppVersion -ProjectRoot $ProjectRoot
$AppBuildLabel = Get-TakSkladBuildLabel -ProjectRoot $ProjectRoot
if ((Compare-TakSkladVersion $AppVersion $MinAppVersion) -lt 0) {
    throw "APP_VERSION $AppVersion is lower than required test minimum $MinAppVersion."
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedBuildLabel) -and $AppBuildLabel -ne $ExpectedBuildLabel) {
    throw "APP_BUILD_LABEL '$AppBuildLabel' does not match expected test label '$ExpectedBuildLabel'."
}

Assert-VersionJsonSafeForTestBuild -ProjectRoot $ProjectRoot

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null

if ($InstallDependencies) {
    Invoke-CheckedCommand -FilePath $Python -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-CheckedCommand -FilePath $Python -Arguments @("-m", "pip", "install", "-r", "requirements.txt")
}

if (-not $SkipTests) {
    Invoke-CheckedCommand -FilePath $Python -Arguments @("-m", "unittest", "discover", "-s", "tests")
}

Invoke-CheckedCommand -FilePath $Python -Arguments @(
    "-m",
    "PyInstaller",
    "--clean",
    "--onedir",
    "--windowed",
    "--paths",
    "src",
    "--add-data",
    "assets\product_images;assets\product_images",
    "--name",
    "TakSklad",
    "--icon",
    "assets\TakSklad.ico",
    "--distpath",
    (Join-Path $BuildDir "dist"),
    "main.py"
)

Invoke-CheckedCommand -FilePath $Python -Arguments @("tools\prepare_acceptance_kit.py")

$PackageName = "TakSklad-test-$AppVersion-windows-x64"
$PackageRoot = Join-Path $OutputDir $PackageName
$ZipPath = "$PackageRoot.zip"
$SourceAppDir = Join-Path $BuildDir "dist\TakSklad"

if (-not (Test-Path $SourceAppDir)) {
    throw "Built app folder not found: $SourceAppDir"
}

if (Test-Path $PackageRoot) {
    Remove-Item $PackageRoot -Recurse -Force
}
if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}

New-Item -ItemType Directory -Path $PackageRoot -Force | Out-Null
$PackagedAppDir = Join-Path $PackageRoot "TakSklad"
New-Item -ItemType Directory -Path $PackagedAppDir -Force | Out-Null
Copy-Item (Join-Path $SourceAppDir "*") $PackagedAppDir -Recurse -Force

$PackagedExe = Join-Path $PackagedAppDir "TakSklad.exe"
if (-not (Test-Path $PackagedExe)) {
    throw "Packaged app does not contain TakSklad.exe: $PackagedExe"
}

New-Item -ItemType Directory -Path (Join-Path $PackageRoot "tools") -Force | Out-Null
Copy-Item "tools\windows_backend_acceptance.ps1" (Join-Path $PackageRoot "tools\windows_backend_acceptance.ps1") -Force
Copy-Item "tools\release_go_no_go.py" (Join-Path $PackageRoot "tools\release_go_no_go.py") -Force

New-Item -ItemType Directory -Path (Join-Path $PackageRoot "acceptance") -Force | Out-Null
Copy-Item "outputs\taksklad_acceptance\README.md" (Join-Path $PackageRoot "acceptance\README.md") -Force
Copy-Item "outputs\taksklad_acceptance\acceptance_manifest.json" (Join-Path $PackageRoot "acceptance\acceptance_manifest.json") -Force
Copy-Item "outputs\taksklad_acceptance\TakSklad_Telegram_Acceptance_2026-05-31.xlsx" (Join-Path $PackageRoot "acceptance\TakSklad_Telegram_Acceptance_2026-05-31.xlsx") -Force
Copy-Item "outputs\taksklad_acceptance\ACCEPTANCE_RESULTS_TEMPLATE.md" (Join-Path $PackageRoot "acceptance\ACCEPTANCE_RESULTS_TEMPLATE.md") -Force
Copy-Item "outputs\taksklad_acceptance\ACCEPTANCE_RESULTS.md" (Join-Path $PackageRoot "acceptance\ACCEPTANCE_RESULTS.md") -Force

if (Test-Path "README.txt") {
    Copy-Item "README.txt" (Join-Path $PackageRoot "README.txt") -Force
}

$BuildManifest = [ordered]@{
    package = $PackageName
    app_version = $AppVersion
    app_build_label = $AppBuildLabel
    package_type = "windows_test_onedir_zip"
    public_version_json_changed = $false
    built_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    acceptance_helper = "tools/windows_backend_acceptance.ps1"
    app_path_for_acceptance = "TakSklad/TakSklad.exe"
}
$BuildManifest | ConvertTo-Json | Out-File (Join-Path $PackageRoot "build_manifest.json") -Encoding utf8
Write-TestBuildReadme -Path (Join-Path $PackageRoot "README_TEST_BUILD.md") -AppVersion $AppVersion -AppBuildLabel $AppBuildLabel -PackageName $PackageName

Assert-TestPackageDoesNotContainLocalSecrets -PackageRoot $PackageRoot

Compress-Archive -Path $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
$Hash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLower()
$Hash | Out-File -FilePath "$ZipPath.sha256.txt" -Encoding ascii -NoNewline

Write-Host "Windows test archive built:"
Write-Host "  $ZipPath"
Write-Host "  $ZipPath.sha256.txt"
Write-Host "SHA256: $Hash"
