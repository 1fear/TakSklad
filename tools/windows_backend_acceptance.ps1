[CmdletBinding()]
param(
    [string]$BackendUrl = "https://api.135.181.245.84.sslip.io",
    [string]$Token = $env:TAKSKLAD_BACKEND_API_TOKEN,
    [int]$TimeoutSeconds = 8,
    [string]$AppPath = "",
    [switch]$UsePython,
    [switch]$CheckOnly,
    [switch]$Clear,
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"

$BackendEnvNames = @(
    "TAKSKLAD_BACKEND_ENABLED",
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    "TAKSKLAD_BACKEND_BASE_URL",
    "TAKSKLAD_BACKEND_API_TOKEN",
    "TAKSKLAD_BACKEND_TIMEOUT_SECONDS"
)

function Show-TakSkladHelp {
    Write-Host "TakSklad Windows backend acceptance helper"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad.exe"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"'
    Write-Host '  .\tools\windows_backend_acceptance.ps1 -Clear'
    Write-Host ""
    Write-Host "The token is used only for this process and child app launch. It is not saved to disk."
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

function Start-TakSkladApp {
    param([string]$ResolvedAppPath)

    $WorkingDirectory = Split-Path -Path $ResolvedAppPath -Parent
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $WorkingDirectory = (Get-Location).Path
    }

    if ($ResolvedAppPath.ToLowerInvariant().EndsWith(".py")) {
        $Python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $Python) {
            $Python = Get-Command py -ErrorAction SilentlyContinue
        }
        if (-not $Python) {
            throw "Python was not found. Pass -AppPath to TakSklad.exe or install Python for this test."
        }
        Start-Process -FilePath $Python.Source -ArgumentList @("`"$ResolvedAppPath`"") -WorkingDirectory $WorkingDirectory
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
} elseif (Test-Path ".\TakSklad.exe") {
    $CandidatePath = (Resolve-Path ".\TakSklad.exe").Path
} elseif (Test-Path ".\main.py") {
    $CandidatePath = (Resolve-Path ".\main.py").Path
} elseif ($UsePython -and (Test-Path ".\main.py")) {
    $CandidatePath = (Resolve-Path ".\main.py").Path
}

if ([string]::IsNullOrWhiteSpace($CandidatePath)) {
    throw "TakSklad app was not found. Run from a folder with TakSklad.exe/main.py or pass -AppPath."
}

Start-TakSkladApp -ResolvedAppPath $CandidatePath
Write-Host "TakSklad launched with backend flags for this child process."
