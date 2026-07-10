[CmdletBinding()]
param(
    [switch]$SyntheticOnly,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Stage = "startup"
$Failure = $null
$MatrixRoot = $null
$AltUserName = $null
$OriginalSyntheticOnly = $env:TAKSKLAD_SYNTHETIC_ONLY
$OriginalSyntheticRoot = $env:TAKSKLAD_SYNTHETIC_ROOT
$OriginalPythonPath = $env:PYTHONPATH
$OriginalNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$OriginalAltUsername = $env:TAKSKLAD_SYNTHETIC_ALT_USERNAME
$OriginalAltPassword = $env:TAKSKLAD_SYNTHETIC_ALT_PASSWORD

function Invoke-CheckedProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "child process failed"
    }
}

function Invoke-Icacls {
    param([string[]]$Arguments)

    $CapturedOutput = & icacls @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ACL command failed"
    }
    $null = $CapturedOutput
}

function Get-SidValue {
    param($IdentityReference)

    $Reference = $IdentityReference
    if ($Reference -isnot [System.Security.Principal.IdentityReference]) {
        $Reference = New-Object System.Security.Principal.NTAccount([string]$Reference)
    }
    return $Reference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
}

function Assert-RestrictedAcl {
    param(
        [string]$Path,
        [string]$CurrentUserSid
    )

    $Acl = Get-Acl -LiteralPath $Path
    if (-not $Acl.AreAccessRulesProtected) {
        throw "ACL inheritance is enabled"
    }
    if ((Get-SidValue -IdentityReference $Acl.Owner) -ne $CurrentUserSid) {
        throw "ACL owner is not the current user"
    }

    $CurrentUserFullControl = $false
    $SystemFullControl = $false
    $FullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    foreach ($Rule in $Acl.Access) {
        if ($Rule.IsInherited) {
            throw "ACL contains an inherited rule"
        }
        if ($Rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
            throw "ACL contains a non-allow rule"
        }
        $RuleSid = Get-SidValue -IdentityReference $Rule.IdentityReference
        if ($RuleSid -notin @($CurrentUserSid, "S-1-5-18")) {
            throw "ACL contains an unexpected principal"
        }
        $HasFullControl = (($Rule.FileSystemRights -band $FullControl) -eq $FullControl)
        if ($RuleSid -eq $CurrentUserSid -and $HasFullControl) {
            $CurrentUserFullControl = $true
        }
        if ($RuleSid -eq "S-1-5-18" -and $HasFullControl) {
            $SystemFullControl = $true
        }
    }
    if (-not $CurrentUserFullControl -or -not $SystemFullControl) {
        throw "ACL is missing required full-control rules"
    }
}

function Resolve-PythonExecutable {
    param(
        [string]$ProjectRoot,
        [string]$RequestedPython
    )

    $Candidates = @(
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    $Command = Get-Command $RequestedPython -ErrorAction SilentlyContinue
    if ($null -eq $Command) {
        throw "Python is unavailable"
    }
    return $Command.Source
}

function Assert-Elevated {
    $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object System.Security.Principal.WindowsPrincipal($Identity)
    if (-not $Principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "elevated disposable Windows runner is required"
    }
}

try {
    $Stage = "platform_guard"
    if (-not $SyntheticOnly) {
        throw "SyntheticOnly is required"
    }
    if ($env:OS -ne "Windows_NT") {
        throw "Windows is required"
    }
    Assert-Elevated

    $Stage = "paths"
    $ProjectRoot = (Split-Path -Path $PSScriptRoot -Parent)
    $PythonPath = Resolve-PythonExecutable -ProjectRoot $ProjectRoot -RequestedPython $Python
    if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
        throw "ProgramData is unavailable"
    }
    $RunId = [guid]::NewGuid().ToString("N")
    $MatrixRoot = Join-Path $env:ProgramData ("TakSklad\phase11-secret-matrix-" + $RunId)
    $StoreDirectory = Join-Path $MatrixRoot "private-store"
    $StoreFile = Join-Path $StoreDirectory "secret_store.v1.dpapi"
    $ControlDirectory = Join-Path $MatrixRoot "control"
    $DigestFile = Join-Path $ControlDirectory "synthetic.sha256"
    $SharedDirectory = Join-Path $MatrixRoot "alternate-probe"
    $CopiedStoreFile = Join-Path $SharedDirectory "copied-secret-store.dpapi"

    New-Item -ItemType Directory -Path $ControlDirectory -Force | Out-Null
    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:TAKSKLAD_SYNTHETIC_ONLY = "1"
    $env:TAKSKLAD_SYNTHETIC_ROOT = $MatrixRoot

    $Stage = "same_user_write"
    Invoke-CheckedProcess -FilePath $PythonPath -Arguments @(
        "-m", "taksklad.secret_store", "synthetic-write",
        "--store-file", $StoreFile,
        "--digest-file", $DigestFile
    )

    $Stage = "same_user_restart"
    Invoke-CheckedProcess -FilePath $PythonPath -Arguments @(
        "-m", "taksklad.secret_store", "synthetic-verify",
        "--store-file", $StoreFile,
        "--digest-file", $DigestFile
    )

    $Stage = "acl_matrix"
    $CurrentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Assert-RestrictedAcl -Path $StoreDirectory -CurrentUserSid $CurrentUserSid
    Assert-RestrictedAcl -Path $StoreFile -CurrentUserSid $CurrentUserSid

    $Stage = "alternate_user_create"
    $AltUserName = "TskDp" + $RunId.Substring(0, 10)
    $AltPasswordPlain = "Aa1!" + $RunId + "Zz9#"
    $AltPassword = ConvertTo-SecureString $AltPasswordPlain -AsPlainText -Force
    New-LocalUser `
        -Name $AltUserName `
        -Password $AltPassword `
        -AccountNeverExpires `
        -PasswordNeverExpires `
        -UserMayNotChangePassword | Out-Null
    $AltUser = Get-LocalUser -Name $AltUserName -ErrorAction Stop
    $AltUserSid = $AltUser.SID.Value
    $AltIdentity = "$env:COMPUTERNAME\$AltUserName"

    $Stage = "alternate_probe_prepare"
    New-Item -ItemType Directory -Path $SharedDirectory -Force | Out-Null
    Invoke-Icacls -Arguments @(
        $SharedDirectory,
        "/inheritance:r",
        "/grant:r",
        ("*" + $CurrentUserSid + ":(OI)(CI)F"),
        ("*S-1-5-18:(OI)(CI)F"),
        ("*" + $AltUserSid + ":(OI)(CI)RX"),
        "/q"
    )
    Copy-Item -LiteralPath $StoreFile -Destination $CopiedStoreFile -Force

    $Stage = "alternate_user_execute"
    $env:TAKSKLAD_SYNTHETIC_ALT_USERNAME = $AltIdentity
    $env:TAKSKLAD_SYNTHETIC_ALT_PASSWORD = $AltPasswordPlain
    & $PythonPath -m taksklad.secret_store synthetic-expect-alternate-denied `
        --private-store-file $StoreFile `
        --copied-store-file $CopiedStoreFile
    if ($LASTEXITCODE -ne 0) {
        if ($LASTEXITCODE -eq 11) {
            $Stage = "alternate_acl_not_denied"
        } elseif ($LASTEXITCODE -eq 12) {
            $Stage = "alternate_dpapi_not_denied"
        } elseif ($LASTEXITCODE -eq 13) {
            $Stage = "alternate_copy_not_readable"
        } elseif ($LASTEXITCODE -eq 14) {
            $Stage = "alternate_dpapi_unexpected_error"
        } elseif ($LASTEXITCODE -eq 20) {
            $Stage = "alternate_identity_missing"
        } elseif ($LASTEXITCODE -eq 21) {
            $Stage = "alternate_logon_failed"
        } elseif ($LASTEXITCODE -eq 22) {
            $Stage = "alternate_impersonation_failed"
        } else {
            $Stage = "alternate_token_setup"
        }
        throw "alternate-user token probe failed"
    }

    $Stage = "complete"
} catch {
    $Failure = [ordered]@{
        Stage = $Stage
        Class = $_.Exception.GetType().Name
    }
} finally {
    $CleanupFailed = $false
    if ($null -ne $AltUserName) {
        try {
            Remove-LocalUser -Name $AltUserName -ErrorAction Stop
        } catch {
            $CleanupFailed = $true
        }
    }
    if ($null -ne $MatrixRoot -and (Test-Path -LiteralPath $MatrixRoot)) {
        try {
            Remove-Item -LiteralPath $MatrixRoot -Recurse -Force -ErrorAction Stop
        } catch {
            $CleanupFailed = $true
        }
    }
    $env:TAKSKLAD_SYNTHETIC_ONLY = $OriginalSyntheticOnly
    $env:TAKSKLAD_SYNTHETIC_ROOT = $OriginalSyntheticRoot
    $env:PYTHONPATH = $OriginalPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $OriginalNoBytecode
    $env:TAKSKLAD_SYNTHETIC_ALT_USERNAME = $OriginalAltUsername
    $env:TAKSKLAD_SYNTHETIC_ALT_PASSWORD = $OriginalAltPassword
    if ($CleanupFailed -and $null -eq $Failure) {
        $Failure = [ordered]@{
            Stage = "cleanup"
            Class = "CleanupError"
        }
    }
}

if ($null -ne $Failure) {
    Write-Error ("windows_secret_store_matrix result=failed stage=" + $Failure.Stage + " class=" + $Failure.Class)
    exit 1
}

Write-Output "windows_secret_store_matrix synthetic_only=true same_user_restart=pass alternate_user_dpapi=denied alternate_user_acl=denied acl=pass cleanup=pass"
exit 0
