[CmdletBinding()]
param(
    [string]$BackendUrl = "https://api.taksklad.uz",
    [string]$Token = $env:TAKSKLAD_BACKEND_API_TOKEN,
    [int]$TimeoutSeconds = 8,
    [string]$AppPath = "",
    [string]$MinAppVersion = "2.0.0",
    [string]$ExpectedAppVersion = "",
    [string]$ExpectedBuildLabel = "MVP 2.0",
    [switch]$BackendOnlyRefresh,
    [switch]$EmergencyGoogleFallback,
    [switch]$EnableDesktopTelegramPolling,
    [switch]$UsePython,
    [switch]$CheckOnly,
    [switch]$Clear,
    [switch]$SkipAppVersionCheck,
    [switch]$SkipBuildLabelCheck,
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"

$BackendEnvNames = @(
    "TAKSKLAD_BACKEND_ENABLED",
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    "TAKSKLAD_BACKEND_BASE_URL",
    "TAKSKLAD_BACKEND_API_TOKEN",
    "TAKSKLAD_BACKEND_TIMEOUT_SECONDS",
    "TAKSKLAD_BACKEND_ONLY_REFRESH",
    "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED",
    "TELEGRAM_DESKTOP_POLLING_ENABLED"
)

function Show-TakSkladHelp {
    Write-Host "TakSklad Windows backend acceptance helper"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad.exe"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -BackendOnlyRefresh -AppPath ".\TakSklad.exe"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -UsePython'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Clear'
    Write-Host ""
    Write-Host "The token is used only for this process and child app launch. It is not saved to disk."
    Write-Host "By default the helper verifies APP_VERSION and APP_BUILD_LABEL so old 1.1.7 shortcuts cannot pass 2.0 acceptance."
    Write-Host "Backend-only shadow mode is workstation-local: -BackendOnlyRefresh sets TAKSKLAD_BACKEND_ONLY_REFRESH=1 only for this child app."
}

function Clear-TakSkladBackendEnv {
    foreach ($Name in $BackendEnvNames) {
        Remove-Item "Env:\$Name" -ErrorAction SilentlyContinue
    }
}

function Invoke-TakSkladBackend {
    param(
        [string]$Method,
        [string]$Path,
        [bool]$Auth
    )

    $Request = @{
        Method = $Method
        Uri = "$BackendUrl$Path"
        TimeoutSec = $TimeoutSeconds
    }
    if ($Auth) {
        $Request.Headers = @{ Authorization = "Bearer $Token" }
    }
    return Invoke-RestMethod @Request
}

function Get-OrderCount {
    param($Orders)

    if ($null -eq $Orders) {
        return 0
    }
    if ($Orders -is [array]) {
        return $Orders.Count
    }
    if ($Orders.PSObject.Properties.Name -contains "Count") {
        return $Orders.Count
    }
    return 1
}

function Get-TakSkladPython {
    param([string]$WorkingDirectory)

    $ProjectRoot = Split-Path -Path $PSScriptRoot -Parent
    $Candidates = @(
        (Join-Path $WorkingDirectory ".venv\Scripts\python.exe"),
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
        (Join-Path (Get-Location).Path ".venv\Scripts\python.exe")
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            return (Resolve-Path $Candidate).Path
        }
    }

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($Python) {
        return $Python.Source
    }

    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        return $PyLauncher.Source
    }

    throw "Python was not found. Pass -AppPath to TakSklad.exe or install Python for this test."
}

function Get-TakSkladSourceBuildInfo {
    param([string]$ResolvedAppPath)

    $ProjectRoot = Split-Path -Path $ResolvedAppPath -Parent
    $ConfigPath = Join-Path $ProjectRoot "src\taksklad\config.py"
    if (-not (Test-Path $ConfigPath)) {
        return $null
    }

    $Text = Get-Content $ConfigPath -Raw
    $Version = ""
    $BuildLabel = ""
    $VersionMatch = [regex]::Match($Text, 'APP_VERSION\s*=\s*"([^"]+)"')
    if ($VersionMatch.Success) {
        $Version = $VersionMatch.Groups[1].Value
    }
    $BuildLabelMatch = [regex]::Match($Text, 'APP_BUILD_LABEL\s*=\s*os\.environ\.get\("TAKSKLAD_BUILD_LABEL",\s*"([^"]*)"\)')
    if (-not $BuildLabelMatch.Success) {
        $BuildLabelMatch = [regex]::Match($Text, 'APP_BUILD_LABEL\s*=\s*"([^"]*)"')
    }
    if ($BuildLabelMatch.Success) {
        $BuildLabel = $BuildLabelMatch.Groups[1].Value
    }

    return @{
        Version = $Version
        BuildLabel = $BuildLabel
    }
}

function Get-TakSkladBuildManifestInfo {
    param([string]$ResolvedAppPath)

    $AppDirectory = Split-Path -Path $ResolvedAppPath -Parent
    $PackageRoot = Split-Path -Path $AppDirectory -Parent
    $Candidates = @(
        (Join-Path $PackageRoot "build_manifest.json"),
        (Join-Path $AppDirectory "build_manifest.json"),
        (Join-Path (Get-Location).Path "build_manifest.json")
    )

    foreach ($Candidate in $Candidates) {
        if (-not (Test-Path $Candidate)) {
            continue
        }

        $Manifest = Get-Content $Candidate -Raw | ConvertFrom-Json
        if ($Manifest.PSObject.Properties.Name -contains "app_version") {
            $BuildLabel = ""
            if ($Manifest.PSObject.Properties.Name -contains "app_build_label") {
                $BuildLabel = [string]$Manifest.app_build_label
            }
            return @{
                Path = (Resolve-Path $Candidate).Path
                Version = [string]$Manifest.app_version
                BuildLabel = $BuildLabel
            }
        }
    }

    return $null
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

function Assert-TakSkladAppVersion {
    param(
        [string]$DetectedVersion,
        [string]$SourceLabel
    )

    if (-not [string]::IsNullOrWhiteSpace($ExpectedAppVersion)) {
        if ($DetectedVersion -ne $ExpectedAppVersion) {
            throw "Unexpected $SourceLabel APP_VERSION: expected=$ExpectedAppVersion detected=$DetectedVersion. Run the current 2.0 test branch or pass -ExpectedAppVersion explicitly."
        }
        Write-Host "$SourceLabel APP_VERSION OK: $DetectedVersion"
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($MinAppVersion)) {
        if ((Compare-TakSkladVersion $DetectedVersion $MinAppVersion) -lt 0) {
            throw "Unexpected $SourceLabel APP_VERSION: minimum=$MinAppVersion detected=$DetectedVersion. Run the current 2.0 test branch or pass -MinAppVersion explicitly."
        }
        Write-Host "$SourceLabel APP_VERSION OK: $DetectedVersion >= $MinAppVersion"
        return
    }

    Write-Host "$SourceLabel APP_VERSION detected: $DetectedVersion"
}

function Assert-TakSkladBuildLabel {
    param(
        [string]$DetectedBuildLabel,
        [string]$SourceLabel
    )

    if ($SkipBuildLabelCheck) {
        Write-Host "$SourceLabel APP_BUILD_LABEL check skipped."
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedBuildLabel)) {
        if ($DetectedBuildLabel -ne $ExpectedBuildLabel) {
            throw "Unexpected $SourceLabel APP_BUILD_LABEL: expected='$ExpectedBuildLabel' detected='$DetectedBuildLabel'. Use a fresh MVP 2.0 test archive or pass -ExpectedBuildLabel/-SkipBuildLabelCheck explicitly."
        }
        Write-Host "$SourceLabel APP_BUILD_LABEL OK: $DetectedBuildLabel"
        return
    }

    Write-Host "$SourceLabel APP_BUILD_LABEL detected: $DetectedBuildLabel"
}

function Test-TakSkladAppVersion {
    param([string]$ResolvedAppPath)

    if ($SkipAppVersionCheck) {
        Write-Host "App version/build label check skipped."
        return
    }

    if ($ResolvedAppPath.ToLowerInvariant().EndsWith(".py")) {
        $BuildInfo = Get-TakSkladSourceBuildInfo -ResolvedAppPath $ResolvedAppPath
        $DetectedVersion = if ($BuildInfo) { [string]$BuildInfo.Version } else { "" }
        if ([string]::IsNullOrWhiteSpace($DetectedVersion)) {
            throw "Could not detect APP_VERSION from source. Pass -SkipAppVersionCheck only if this is intentional."
        }
        Assert-TakSkladAppVersion -DetectedVersion $DetectedVersion -SourceLabel "Source"
        Assert-TakSkladBuildLabel -DetectedBuildLabel ([string]$BuildInfo.BuildLabel) -SourceLabel "Source"
        return
    }

    $BuildManifest = Get-TakSkladBuildManifestInfo -ResolvedAppPath $ResolvedAppPath
    if ($BuildManifest) {
        Assert-TakSkladAppVersion -DetectedVersion $BuildManifest.Version -SourceLabel "Test build"
        Assert-TakSkladBuildLabel -DetectedBuildLabel ([string]$BuildManifest.BuildLabel) -SourceLabel "Test build"
        Write-Host "Build manifest: $($BuildManifest.Path)"
        return
    }

    throw "Cannot verify TakSklad.exe version because build_manifest.json was not found. Use a fresh test archive built by tools\build_windows_test_archive.ps1, run from source main.py, or pass -SkipAppVersionCheck only for an intentional manual check."
}

function Start-TakSkladApp {
    param([string]$ResolvedAppPath)

    $WorkingDirectory = Split-Path -Path $ResolvedAppPath -Parent
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $WorkingDirectory = (Get-Location).Path
    }

    if ($ResolvedAppPath.ToLowerInvariant().EndsWith(".py")) {
        $PythonPath = Get-TakSkladPython -WorkingDirectory $WorkingDirectory
        Write-Host "Python: $PythonPath"
        Start-Process -FilePath $PythonPath -ArgumentList @("`"$ResolvedAppPath`"") -WorkingDirectory $WorkingDirectory
        return
    }

    Start-Process -FilePath $ResolvedAppPath -WorkingDirectory $WorkingDirectory
}

if ($ShowHelp) {
    Show-TakSkladHelp
    exit 0
}

if ($Clear) {
    Clear-TakSkladBackendEnv
    Write-Host "TakSklad backend env was cleared for the current PowerShell process."
    exit 0
}

$BackendUrl = $BackendUrl.Trim().TrimEnd("/")
if ([string]::IsNullOrWhiteSpace($BackendUrl)) {
    throw "BackendUrl is required."
}
if ([string]::IsNullOrWhiteSpace($Token)) {
    throw 'Service token is required. Pass -Token or set $env:TAKSKLAD_BACKEND_API_TOKEN before running this script.'
}
if ($TimeoutSeconds -lt 1) {
    throw "TimeoutSeconds must be greater than 0."
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Write-Host "Checking backend health: $BackendUrl/health"
$Health = Invoke-TakSkladBackend -Method "GET" -Path "/health" -Auth $false
Write-Host "Backend health OK: status=$($Health.status) service=$($Health.service)"

Write-Host "Checking active orders with service token"
$ActiveOrders = Invoke-TakSkladBackend -Method "GET" -Path "/api/v1/orders/active" -Auth $true
$ActiveOrderCount = Get-OrderCount -Orders $ActiveOrders
Write-Host "Active orders OK: $ActiveOrderCount"

$env:TAKSKLAD_BACKEND_ENABLED = "1"
$env:TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = "1"
$env:TAKSKLAD_BACKEND_BASE_URL = $BackendUrl
$env:TAKSKLAD_BACKEND_API_TOKEN = $Token
$env:TAKSKLAD_BACKEND_TIMEOUT_SECONDS = [string]$TimeoutSeconds
$env:TAKSKLAD_BACKEND_ONLY_REFRESH = if ($BackendOnlyRefresh) { "1" } else { "0" }
$env:TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = if ($EmergencyGoogleFallback) { "1" } else { "0" }
$env:TELEGRAM_DESKTOP_POLLING_ENABLED = if ($EnableDesktopTelegramPolling) { "1" } else { "0" }

Write-Host "Backend-only refresh: $env:TAKSKLAD_BACKEND_ONLY_REFRESH"
Write-Host "Emergency Google fallback: $env:TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED"
Write-Host "Desktop Telegram polling: $env:TELEGRAM_DESKTOP_POLLING_ENABLED"

if ($CheckOnly) {
    Write-Host "CheckOnly finished. No app was launched."
    exit 0
}

$CandidatePath = ""
if (-not [string]::IsNullOrWhiteSpace($AppPath)) {
    if (-not (Test-Path $AppPath)) {
        throw "AppPath was not found: $AppPath"
    }
    $CandidatePath = (Resolve-Path $AppPath).Path
} elseif ($UsePython -and (Test-Path ".\main.py")) {
    $CandidatePath = (Resolve-Path ".\main.py").Path
} elseif (Test-Path ".\TakSklad.exe") {
    $CandidatePath = (Resolve-Path ".\TakSklad.exe").Path
} elseif (Test-Path ".\main.py") {
    $CandidatePath = (Resolve-Path ".\main.py").Path
}

if ([string]::IsNullOrWhiteSpace($CandidatePath)) {
    throw "TakSklad app was not found. Run from a folder with TakSklad.exe/main.py or pass -AppPath."
}

Write-Host "App path: $CandidatePath"
Test-TakSkladAppVersion -ResolvedAppPath $CandidatePath

Start-TakSkladApp -ResolvedAppPath $CandidatePath
Write-Host "TakSklad launched with backend flags for this child process."
