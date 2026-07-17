[CmdletBinding()]
param(
    [string]$BackendUrl = "https://api.taksklad.uz",
    [int]$TimeoutSeconds = 8,
    [string]$AppPath = "",
    [string]$MinAppVersion = "2.0.0",
    [string]$ExpectedAppVersion = "",
    [string]$ExpectedBuildLabel = "MVP 2.0",
    [switch]$CheckOnly,
    [switch]$Clear,
    [switch]$InstallBackendToken,
    [string]$PrincipalIdentifier = "",
    [switch]$SkipAppVersionCheck,
    [switch]$SkipBuildLabelCheck,
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"

$BackendEnvNames = @(
    "TAKSKLAD_BACKEND_ENABLED",
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    "TAKSKLAD_BACKEND_BASE_URL",
    "TAKSKLAD_BACKEND_TIMEOUT_SECONDS",
    "TAKSKLAD_BACKEND_ONLY_REFRESH"
)
$LegacyCleanupOnlyEnvNames = @(
    "TAKSKLAD_BACKEND_API_TOKEN"
)
$ApprovedProductionBackendOrigin = "https://api.taksklad.uz"
$PinnedProductionSignerCertificateSha256 = "c95ccd968831b3b55a1f2c949e66f3b39c5f69badf29a70887b43a036f14bb19"

function Show-TakSkladHelp {
    Write-Host "TakSklad Windows backend acceptance helper"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host '  .\windows_backend_acceptance.ps1 -CheckOnly -AppPath ".\TakSklad.exe"'
    Write-Host '  .\windows_backend_acceptance.ps1 -AppPath ".\TakSklad.exe"'
    Write-Host '  .\windows_backend_acceptance.ps1 -Clear'
    Write-Host ""
    Write-Host "The canary reads the same current-user DPAPI store as the production desktop."
    Write-Host "Credential installation is a separate stdin-only TakSkladAuth.exe command; tokens are never accepted as arguments."
    Write-Host "By default the helper verifies APP_VERSION and APP_BUILD_LABEL so old 1.1.7 shortcuts cannot pass 2.0 acceptance."
    Write-Host "TakSklad desktop is backend-only; no legacy storage fallback can be enabled by this helper."
}

function Clear-TakSkladBackendEnv {
    foreach ($Name in @($BackendEnvNames + $LegacyCleanupOnlyEnvNames)) {
        Remove-Item "Env:\$Name" -ErrorAction SilentlyContinue
    }
}

function Get-TakSkladBuildManifestInfo {
    param([string]$ResolvedAppPath)

    $AppDirectory = Split-Path -Path $ResolvedAppPath -Parent
    $Candidates = @((Join-Path $AppDirectory "build_manifest.json"))

    foreach ($Candidate in $Candidates) {
        if (-not (Test-Path $Candidate)) {
            continue
        }

        $Manifest = Get-Content $Candidate -Raw | ConvertFrom-Json
        if (($Manifest.PSObject.Properties.Name -contains "app_version") -or ($Manifest.PSObject.Properties.Name -contains "latest_version")) {
            $BuildLabel = ""
            if ($Manifest.PSObject.Properties.Name -contains "app_build_label") {
                $BuildLabel = [string]$Manifest.app_build_label
            }
            return @{
                Path = (Resolve-Path $Candidate).Path
                Version = [string]$(if ($Manifest.app_version) { $Manifest.app_version } else { $Manifest.latest_version })
                BuildLabel = $BuildLabel
                AppPath = [string]$Manifest.app_path_for_acceptance
                AppSha256 = [string]$Manifest.app_sha256
                AcceptanceWrapper = [string]$Manifest.acceptance_wrapper
                AcceptanceWrapperSha256 = [string]$Manifest.acceptance_wrapper_sha256
                AuthHelperSha256 = [string]$Manifest.auth_helper_sha256
                SignatureRequired = [bool]$Manifest.signature_required
                SignerCertificateSha256 = [string]$Manifest.signer_certificate_sha256
            }
        }
    }

    return $null
}

function Assert-TakSkladAuthHelperIntegrity {
    param(
        [string]$ResolvedAppPath,
        [string]$ResolvedAuthHelperPath
    )

    $BuildManifest = Get-TakSkladBuildManifestInfo -ResolvedAppPath $ResolvedAppPath
    if (-not $BuildManifest -or [string]::IsNullOrWhiteSpace($BuildManifest.AuthHelperSha256)) {
        throw "Package manifest does not pin TakSkladAuth.exe SHA256."
    }
    if ($BuildManifest.AppPath -ne "TakSklad.exe" -or
        [IO.Path]::GetFileName($ResolvedAppPath) -ne "TakSklad.exe" -or
        [string]::IsNullOrWhiteSpace($BuildManifest.AppSha256)) {
        throw "Package manifest does not bind the selected TakSklad.exe identity."
    }
    $ActualAppHash = (Get-FileHash -LiteralPath $ResolvedAppPath -Algorithm SHA256).Hash.ToLower()
    if ($ActualAppHash -ne $BuildManifest.AppSha256.ToLower()) {
        throw "TakSklad.exe SHA256 differs from the package manifest."
    }
    if ($BuildManifest.AcceptanceWrapper -ne "windows_backend_acceptance.ps1" -or
        [string]::IsNullOrWhiteSpace($BuildManifest.AcceptanceWrapperSha256)) {
        throw "Package manifest does not bind the acceptance wrapper."
    }
    $ActualWrapperHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLower()
    if ($ActualWrapperHash -ne $BuildManifest.AcceptanceWrapperSha256.ToLower()) {
        throw "Acceptance wrapper SHA256 differs from the package manifest."
    }
    if (-not $BuildManifest.SignatureRequired) {
        throw "Unsigned/local test archives are synthetic-only and cannot use production DPAPI credentials."
    }
    if ($BuildManifest.SignerCertificateSha256.ToLower() -ne $PinnedProductionSignerCertificateSha256) {
        throw "Package manifest signer is not the pinned production signer."
    }
    $ActualHash = (Get-FileHash -LiteralPath $ResolvedAuthHelperPath -Algorithm SHA256).Hash.ToLower()
    if ($ActualHash -ne $BuildManifest.AuthHelperSha256.ToLower()) {
        throw "TakSkladAuth.exe SHA256 differs from the package manifest."
    }
    Assert-TakSkladPinnedAuthenticodeSignature -ArtifactPath $ResolvedAppPath -SourceLabel "TakSklad.exe"
    Assert-TakSkladPinnedAuthenticodeSignature -ArtifactPath $ResolvedAuthHelperPath -SourceLabel "TakSkladAuth.exe"
}

function Assert-TakSkladPinnedAuthenticodeSignature {
    param(
        [string]$ArtifactPath,
        [string]$SourceLabel
    )

    $Signature = Get-AuthenticodeSignature -LiteralPath $ArtifactPath
    if ($null -eq $Signature.SignerCertificate) {
        throw "$SourceLabel Authenticode signer is missing."
    }
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $SignerSha256 = -join ($Hasher.ComputeHash($Signature.SignerCertificate.RawData) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $Hasher.Dispose()
    }
    if ($SignerSha256 -ne $PinnedProductionSignerCertificateSha256) {
        throw "$SourceLabel is not signed by the pinned production signer."
    }

    $Chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
    try {
        $Chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
        $null = $Chain.Build($Signature.SignerCertificate)
        $ChainStatuses = @($Chain.ChainStatus | ForEach-Object { $_.Status.ToString() } | Sort-Object -Unique)
    } finally {
        $Chain.Dispose()
    }
    $AcceptedSignatureStatuses = @(
        [System.Management.Automation.SignatureStatus]::Valid,
        [System.Management.Automation.SignatureStatus]::NotTrusted,
        [System.Management.Automation.SignatureStatus]::UnknownError
    )
    if ($AcceptedSignatureStatuses -notcontains $Signature.Status) {
        throw "$SourceLabel Authenticode status is not accepted."
    }
    $AcceptedChainStatuses = @('PartialChain', 'UntrustedRoot')
    if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -and
        ($ChainStatuses.Count -ne 1 -or $AcceptedChainStatuses -notcontains $ChainStatuses[0])) {
        throw "$SourceLabel Authenticode chain is not accepted."
    }
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

    $BuildManifest = Get-TakSkladBuildManifestInfo -ResolvedAppPath $ResolvedAppPath
    if ($BuildManifest) {
        Assert-TakSkladAppVersion -DetectedVersion $BuildManifest.Version -SourceLabel "Test build"
        Assert-TakSkladBuildLabel -DetectedBuildLabel ([string]$BuildManifest.BuildLabel) -SourceLabel "Test build"
        Write-Host "Build manifest: $($BuildManifest.Path)"
        return
    }

        throw "Cannot verify TakSklad.exe because its signed package build_manifest.json was not found."
}

function Start-TakSkladApp {
    param([string]$ResolvedAppPath)

    $WorkingDirectory = Split-Path -Path $ResolvedAppPath -Parent
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $WorkingDirectory = (Get-Location).Path
    }

    Start-Process -FilePath $ResolvedAppPath -WorkingDirectory $WorkingDirectory
}

function Invoke-TakSkladAuthCanary {
    param(
        [string]$ResolvedAppPath,
        [string]$ResolvedAuthHelperPath
    )

    $WorkingDirectory = Split-Path -Path $ResolvedAppPath -Parent
    & $ResolvedAuthHelperPath --auth-canary
    if ($LASTEXITCODE -ne 0) {
        throw "Credentialed read-only returns canary failed."
    }
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
if ($TimeoutSeconds -lt 1) {
    throw "TimeoutSeconds must be greater than 0."
}
if ($BackendUrl -ne $ApprovedProductionBackendOrigin) {
    throw "BackendUrl must match the approved production origin for credentialed acceptance."
}

$env:TAKSKLAD_BACKEND_ENABLED = "1"
$env:TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = "1"
$env:TAKSKLAD_BACKEND_BASE_URL = $BackendUrl
$env:TAKSKLAD_BACKEND_TIMEOUT_SECONDS = [string]$TimeoutSeconds
$env:TAKSKLAD_BACKEND_ONLY_REFRESH = "1"

Write-Host "Backend-only refresh: $env:TAKSKLAD_BACKEND_ONLY_REFRESH"

$CandidatePath = ""
if (-not [string]::IsNullOrWhiteSpace($AppPath)) {
    if (-not (Test-Path $AppPath)) {
        throw "AppPath was not found: $AppPath"
    }
    $CandidatePath = (Resolve-Path $AppPath).Path
} elseif (Test-Path ".\TakSklad.exe") {
    $CandidatePath = (Resolve-Path ".\TakSklad.exe").Path
}

if ([string]::IsNullOrWhiteSpace($CandidatePath)) {
    throw "Signed TakSklad.exe was not found. Run from the signed release folder or pass -AppPath."
}
if ($CandidatePath.ToLowerInvariant().EndsWith(".py")) {
    throw "Production DPAPI acceptance is packaged-only; source tests must inject a synthetic store and localhost test API."
}
$WrapperDirectory = (Resolve-Path (Split-Path -Path $PSCommandPath -Parent)).Path
$AppDirectory = (Resolve-Path (Split-Path -Path $CandidatePath -Parent)).Path
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($WrapperDirectory, $AppDirectory)) {
    throw "TakSklad.exe must be adjacent to the verified packaged acceptance wrapper."
}

Write-Host "App path: $CandidatePath"
Test-TakSkladAppVersion -ResolvedAppPath $CandidatePath
$AuthHelperPath = Join-Path (Split-Path -Path $CandidatePath -Parent) "TakSkladAuth.exe"
if (-not (Test-Path $AuthHelperPath)) {
    throw "Signed TakSkladAuth.exe was not found adjacent to the selected app."
}
$ResolvedAuthHelperPath = (Resolve-Path $AuthHelperPath).Path
Assert-TakSkladAuthHelperIntegrity -ResolvedAppPath $CandidatePath -ResolvedAuthHelperPath $ResolvedAuthHelperPath
Write-Host "Auth helper path: $ResolvedAuthHelperPath"
if ($InstallBackendToken) {
    if ($PrincipalIdentifier -notmatch '^[a-z0-9][a-z0-9._-]{2,119}$') {
        throw "A valid expected desktop principal identifier is required before credential installation."
    }
    $PreviousPrincipalIdentifier = [Environment]::GetEnvironmentVariable("TAKSKLAD_DESKTOP_PRINCIPAL_IDENTIFIER", "Process")
    [Environment]::SetEnvironmentVariable("TAKSKLAD_DESKTOP_PRINCIPAL_IDENTIFIER", $PrincipalIdentifier, "Process")
    $SecureToken = $null
    $TokenPtr = [IntPtr]::Zero
    try {
        $SecureToken = Read-Host "Scoped desktop token" -AsSecureString
        $TokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
        $TokenText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPtr)
        $TokenText | & $ResolvedAuthHelperPath --install-backend-token-stdin
        if ($LASTEXITCODE -ne 0) {
            throw "Scoped desktop credential installation failed."
        }
    } finally {
        $TokenText = $null
        if ($TokenPtr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPtr)
        }
        if ($null -ne $SecureToken) {
            $SecureToken.Dispose()
        }
        [Environment]::SetEnvironmentVariable(
            "TAKSKLAD_DESKTOP_PRINCIPAL_IDENTIFIER",
            $PreviousPrincipalIdentifier,
            "Process"
        )
    }
}
Write-Host "Checking the data-free desktop readiness endpoint via current-user DPAPI"
Invoke-TakSkladAuthCanary -ResolvedAppPath $CandidatePath -ResolvedAuthHelperPath $ResolvedAuthHelperPath

if ($CheckOnly) {
    Write-Host "CheckOnly finished. No app was launched."
    exit 0
}

Start-TakSkladApp -ResolvedAppPath $CandidatePath
Write-Host "TakSklad launched with backend flags and its persisted DPAPI credential."
