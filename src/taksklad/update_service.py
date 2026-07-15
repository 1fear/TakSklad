import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

from .config import (
    APP_DIR,
    APP_EXECUTABLE_NAME,
    APP_NAME,
    APP_VERSION,
    UPDATE_CHECK_TIMEOUT_SECONDS,
    UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
    UPDATE_INFO_URL,
    UPDATE_LOG_FILE,
)
from .http_client import open_https_url
from .utils import file_sha256, normalize_text

UPDATE_RUNTIME_EXCLUDE_FILES = (
    "TakSklad_data.json",
    "TakSklad_data.json.last_good.*.bak",
    "TakSklad_data.json.*.tmp",
    "TakSklad_queues.sqlite3",
    "TakSklad_queues.sqlite3-wal",
    "TakSklad_queues.sqlite3-shm",
    "credentials.json",
    "telegram_settings.json",
    "yandex_geocoder_key.txt",
    ".env.taksklad-vds-2.0.generated.json",
    "secret-store-v1.json",
    "secret_store.v1.dpapi",
    "pending_saves.json",
    "pending_prints.json",
    "pending_telegram.json",
    "pending_backend_events.json",
    "telegram_state.json",
    "product_catalog.json",
    "import_history.json",
    "print_settings.json",
    "*.log",
)
UPDATE_RUNTIME_SECRET_FILES = (
    "credentials.json",
    "telegram_settings.json",
    "yandex_geocoder_key.txt",
    ".env.taksklad-vds-2.0.generated.json",
    "secret-store-v1.json",
    "secret_store.v1.dpapi",
)
UPDATE_RUNTIME_PRESERVE_FILES = tuple(
    name for name in UPDATE_RUNTIME_EXCLUDE_FILES if name not in UPDATE_RUNTIME_SECRET_FILES
)
UPDATE_RUNTIME_EXCLUDE_DIRS = (
    "scan_backups",
    "reports",
    "outputs",
    "backups",
    "diagnostics",
)
MAX_UPDATE_DOWNLOAD_BYTES = 512 * 1024 * 1024
UPDATE_SIGNATURE_TYPE = "authenticode"
WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE = "WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE"
# The release certificate chains to TakSklad's internal CA, which is intentionally
# not installed into warehouse workstations. Depending on Windows, PowerShell
# reports NotTrusted or UnknownError on a clean PC. Those statuses are accepted
# only when a no-revocation X509Chain check fails solely with PartialChain and the
# exact compiled-in leaf certificate fingerprint matches. HashMismatch, missing
# signatures, expired certificates, and every other chain status remain closed.
WINDOWS_AUTHENTICODE_PINNED_STATUSES = frozenset({"Valid", "NotTrusted", "UnknownError"})
WINDOWS_AUTHENTICODE_PINNED_CHAIN_STATUSES = frozenset({"PartialChain"})
# Public certificate fingerprints are release inputs, not secrets. Keep this
# fail-closed until the production certificate is approved and its SHA-256
# fingerprint is compiled into the released application.
TRUSTED_WINDOWS_SIGNER_CERT_SHA256 = frozenset(
    {"c95ccd968831b3b55a1f2c949e66f3b39c5f69badf29a70887b43a036f14bb19"}
)
IMMUTABLE_RELEASE_TAG_RE = re.compile(
    r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)


def parse_version_parts(version):
    parts = re.findall(r"\d+", normalize_text(version))
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts[:4])

def compare_versions(left, right):
    left_parts = parse_version_parts(left)
    right_parts = parse_version_parts(right)
    max_len = max(len(left_parts), len(right_parts))
    left_parts = left_parts + (0,) * (max_len - len(left_parts))
    right_parts = right_parts + (0,) * (max_len - len(right_parts))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0

def get_runtime_package_type():
    if not getattr(sys, "frozen", False):
        return "source"
    meipass = os.path.abspath(getattr(sys, "_MEIPASS", ""))
    app_dir = os.path.abspath(APP_DIR)
    if meipass:
        try:
            if os.path.commonpath([app_dir, meipass]) == app_dir:
                return "onedir"
        except ValueError:
            pass
    return "onefile"

def manifest_targets_onedir(update_info):
    package_type = normalize_text(update_info.get("package_type")).lower()
    return package_type in ("onedir", "onedir_zip", "zip")

def package_transition_required(update_info):
    return (
        getattr(sys, "frozen", False)
        and manifest_targets_onedir(update_info)
        and get_runtime_package_type() != "onedir"
        and bool(normalize_text(update_info.get("download_url_onedir")))
    )

def fetch_update_info():
    if not UPDATE_INFO_URL:
        return None

    separator = "&" if "?" in UPDATE_INFO_URL else "?"
    url = f"{UPDATE_INFO_URL}{separator}_={int(datetime.now().timestamp())}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )
    with open_https_url(request, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:
        update_info = json.load(response)

    if not isinstance(update_info, dict):
        raise ValueError("Файл обновления должен быть JSON-объектом")
    return update_info

def select_update_download(update_info):
    if manifest_targets_onedir(update_info):
        onedir_url = normalize_text(update_info.get("download_url_onedir"))
        if onedir_url:
            return onedir_url, normalize_text(update_info.get("sha256_onedir")).lower()
    return (
        normalize_text(update_info.get("download_url")),
        normalize_text(update_info.get("sha256")).lower(),
    )

def validate_update_download_url(download_url):
    parsed_url = urllib.parse.urlparse(download_url)
    if parsed_url.scheme != "https" or parsed_url.netloc.lower() != "github.com":
        raise ValueError("download_url обновления должен быть HTTPS-ссылкой GitHub Releases")
    if parsed_url.username or parsed_url.password:
        raise ValueError("download_url обновления не должен содержать логин или пароль")
    if parsed_url.query or parsed_url.fragment:
        raise ValueError("download_url обновления не должен содержать query или fragment")
    path_parts = parsed_url.path.split("/")
    if len(path_parts) != 7 or path_parts[1:5] != ["1fear", "TakSklad", "releases", "download"]:
        raise ValueError("download_url обновления должен вести на release 1fear/TakSklad")
    release_tag = urllib.parse.unquote(path_parts[5])
    asset_name = urllib.parse.unquote(path_parts[6])
    if not IMMUTABLE_RELEASE_TAG_RE.fullmatch(release_tag):
        raise ValueError("download_url обновления должен содержать immutable version release tag")
    if not asset_name or asset_name in {".", ".."} or "/" in asset_name or "\\" in asset_name:
        raise ValueError("download_url обновления должен содержать имя release asset")


def update_release_tag(download_url):
    validate_update_download_url(download_url)
    return urllib.parse.unquote(urllib.parse.urlparse(download_url).path.split("/")[5])

def validate_update_sha256(expected_sha256):
    if not expected_sha256:
        raise ValueError("SHA256 обновления обязателен")
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        raise ValueError("SHA256 обновления в version.json должен быть lowercase hex digest")


def validate_update_manifest(update_info, trusted_signers=None):
    if not isinstance(update_info, dict):
        raise ValueError("Файл обновления должен быть JSON-объектом")
    download_url, expected_sha256 = select_update_download(update_info)
    if not download_url:
        raise ValueError("В version.json не указан download_url для обновления")
    validate_update_download_url(download_url)
    validate_update_sha256(expected_sha256)

    latest_version = normalize_text(update_info.get("latest_version"))
    if not latest_version:
        raise ValueError("В version.json не указан latest_version")
    release_tag = normalize_text(update_info.get("release_tag"))
    if not IMMUTABLE_RELEASE_TAG_RE.fullmatch(release_tag):
        raise ValueError("Manifest обновления должен быть привязан к immutable release tag")
    if release_tag != f"v{latest_version}" or update_release_tag(download_url) != release_tag:
        raise ValueError("Release tag обновления не совпадает с latest_version")

    signature_type = normalize_text(update_info.get("signature_type")).lower()
    if signature_type != UPDATE_SIGNATURE_TYPE or update_info.get("signature_required") is not True:
        raise ValueError("Для обновления обязательна Authenticode-подпись")
    signer_certificate_sha256 = normalize_text(
        update_info.get("signer_certificate_sha256")
    ).lower()
    validate_update_sha256(signer_certificate_sha256)
    allowed_signers = frozenset(
        normalize_text(value).lower()
        for value in (
            TRUSTED_WINDOWS_SIGNER_CERT_SHA256
            if trusted_signers is None
            else trusted_signers
        )
    )
    if not allowed_signers:
        raise RuntimeError(WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE)
    if signer_certificate_sha256 not in allowed_signers:
        raise ValueError("Сертификат издателя обновления не входит в доверенный allowlist")
    return download_url, expected_sha256, signer_certificate_sha256


def verify_windows_authenticode_signature(artifact_path, expected_signer_certificate_sha256):
    if os.name != "nt":
        raise RuntimeError("WINDOWS_AUTHENTICODE_VERIFICATION_UNAVAILABLE")
    validate_update_sha256(expected_signer_certificate_sha256)
    script = (
        "& { param([string]$Path) "
        "$signature = Get-AuthenticodeSignature -LiteralPath $Path; "
        "$status = $signature.Status.ToString(); "
        "$fingerprint = ''; "
        "$chainStatuses = @(); "
        "if ($null -ne $signature.SignerCertificate) { "
        "$hasher = [System.Security.Cryptography.SHA256]::Create(); "
        "try { $bytes = $hasher.ComputeHash($signature.SignerCertificate.RawData) } "
        "finally { $hasher.Dispose() }; "
        "$fingerprint = -join ($bytes | ForEach-Object { $_.ToString('x2') }); "
        "$chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain; "
        "try { $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck; "
        "$null = $chain.Build($signature.SignerCertificate); "
        "$chainStatuses = @($chain.ChainStatus | ForEach-Object { $_.Status.ToString() } | Sort-Object -Unique) } "
        "finally { $chain.Dispose() } }; "
        "Write-Output $status; Write-Output $fingerprint; "
        "Write-Output ('CHAIN:' + ($chainStatuses -join ',')) }"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
            artifact_path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    signature_status = output_lines[0] if output_lines else ""
    signer_certificate_sha256 = output_lines[1].lower() if len(output_lines) >= 2 else ""
    chain_line = output_lines[2] if len(output_lines) == 3 else ""
    chain_statuses = frozenset(
        status for status in chain_line.removeprefix("CHAIN:").split(",") if status
    ) if chain_line.startswith("CHAIN:") else frozenset()
    signature_is_valid = signature_status == "Valid"
    signature_is_pinned_internal_ca = (
        signature_status in WINDOWS_AUTHENTICODE_PINNED_STATUSES - {"Valid"}
        and chain_statuses == WINDOWS_AUTHENTICODE_PINNED_CHAIN_STATUSES
    )
    if completed.returncode != 0 or not (
        signature_is_valid or signature_is_pinned_internal_ca
    ):
        status_detail = signature_status or "Unknown"
        raise ValueError(
            f"Authenticode-подпись обновления недействительна: {status_detail}"
        )
    if signer_certificate_sha256 != expected_signer_certificate_sha256:
        raise ValueError("Authenticode-подпись создана недоверенным издателем")
    return True

def download_update_file(update_info, trusted_signers=None):
    download_url, expected_sha256, signer_certificate_sha256 = validate_update_manifest(
        update_info,
        trusted_signers=trusted_signers,
    )

    parsed_url = urllib.parse.urlparse(download_url)
    suffix = os.path.splitext(parsed_url.path)[1] or ".exe"
    temp_file = tempfile.NamedTemporaryFile(prefix=f"{APP_NAME}_update_", suffix=suffix, delete=False)
    temp_path = temp_file.name
    temp_file.close()

    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
    )
    try:
        with open_https_url(request, timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS) as response:
            content_length = normalize_text(response.headers.get("Content-Length"))
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise ValueError("Content-Length обновления должен быть целым числом") from exc
                if declared_size < 0 or declared_size > MAX_UPDATE_DOWNLOAD_BYTES:
                    raise ValueError("Размер обновления превышает допустимый лимит")
            downloaded_size = 0
            with open(temp_path, "wb") as file_obj:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded_size += len(chunk)
                    if downloaded_size > MAX_UPDATE_DOWNLOAD_BYTES:
                        raise ValueError("Размер обновления превышает допустимый лимит")
                    file_obj.write(chunk)

        actual_sha256 = file_sha256(temp_path)
        if actual_sha256.lower() != expected_sha256:
            raise ValueError("Контрольная сумма обновления не совпала")

        if not manifest_targets_onedir(update_info):
            verify_windows_authenticode_signature(
                temp_path,
                signer_certificate_sha256,
            )

        return temp_path
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise

def detect_update_package_type(update_info, downloaded_path):
    package_type = normalize_text(update_info.get("package_type")).lower()
    if package_type:
        return package_type
    if downloaded_path.lower().endswith(".zip"):
        return "onedir_zip"
    return "onefile_exe"

def validate_onedir_zip(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as zip_file:
            names = [name.replace("\\", "/") for name in zip_file.namelist()]
    except zipfile.BadZipFile as exc:
        raise ValueError("Файл обновления повреждён или не является ZIP-архивом") from exc

    candidates = (
        APP_EXECUTABLE_NAME,
        f"{APP_NAME}/{APP_EXECUTABLE_NAME}",
        f"./{APP_EXECUTABLE_NAME}",
        f"./{APP_NAME}/{APP_EXECUTABLE_NAME}",
    )
    normalized = {name.lstrip("/") for name in names}
    if not any(candidate in normalized for candidate in candidates):
        raise ValueError(f"ZIP-обновление не содержит {APP_EXECUTABLE_NAME}")

def powershell_single_quoted(value):
    return "'" + str(value).replace("'", "''") + "'"

def powershell_array(values):
    return "@(" + ", ".join(powershell_single_quoted(value) for value in values) + ")"

def get_windows_desktop_dir():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
        if result == 0 and buffer.value:
            return buffer.value
    except Exception:
        logging.debug("Не удалось получить путь Desktop через SHGetFolderPathW", exc_info=True)
    return os.path.join(os.path.expanduser("~"), "Desktop")

def write_windows_shortcut_script(target_exe=None, working_dir=None, shortcut_path=None, shortcut_path_expression=None):
    target_exe = target_exe or sys.executable
    working_dir = working_dir or os.path.dirname(target_exe)
    if shortcut_path is None and shortcut_path_expression is None:
        desktop_dir = get_windows_desktop_dir()
        if not desktop_dir:
            raise RuntimeError("Не удалось определить рабочий стол Windows")
        shortcut_path = os.path.join(desktop_dir, f"{APP_NAME}.lnk")
    shortcut_path_line = (
        f"$shortcutPath = {shortcut_path_expression}"
        if shortcut_path_expression
        else f"$shortcutPath = {powershell_single_quoted(shortcut_path)}"
    )

    return f"""$ErrorActionPreference = 'Stop'
{shortcut_path_line}
$targetPath = {powershell_single_quoted(target_exe)}
$workingDirectory = {powershell_single_quoted(working_dir)}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $targetPath
$shortcut.WorkingDirectory = $workingDirectory
$shortcut.IconLocation = "$targetPath,0"
$shortcut.Description = '{APP_NAME}: складское приложение'
$shortcut.Save()
"""

def ensure_windows_desktop_shortcut():
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return False
    try:
        desktop_dir = get_windows_desktop_dir()
        if not desktop_dir:
            return False
        os.makedirs(desktop_dir, exist_ok=True)
        shortcut_path = os.path.join(desktop_dir, f"{APP_NAME}.lnk")
        script = write_windows_shortcut_script(shortcut_path=shortcut_path)
        script_path = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_shortcut_{os.getpid()}.ps1")
        with open(script_path, "w", encoding="utf-8-sig") as script_file:
            script_file.write(script)
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            timeout=20,
        )
        try:
            os.remove(script_path)
        except OSError:
            pass
        if completed.returncode != 0:
            logging.warning("Не удалось создать ярлык %s: %s", shortcut_path, completed.stderr.decode("utf-8", "replace"))
            return False
        logging.info("Ярлык приложения проверен: %s", shortcut_path)
        return True
    except Exception:
        logging.exception("Не удалось создать ярлык приложения на рабочем столе")
        return False

def create_windows_exe_updater(new_exe_path):
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Автообновление доступно только в собранной Windows-версии приложения")
    if os.name != "nt":
        raise RuntimeError("Автообновление сейчас поддерживает только Windows exe")

    current_exe = sys.executable
    updater_path = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_updater_{os.getpid()}.bat")
    log_path = UPDATE_LOG_FILE
    # ВАЖНО: при ошибке копирования НЕЛЬЗЯ перезапускать старый exe.
    # Старый exe снова обнаружит «нужно обновиться», снова запустит этот
    # же updater, который снова упадёт — получится бесконечный цикл
    # «приложение само открывается после закрытия». Поэтому пишем ошибку
    # в лог и выходим, ничего не запуская. Пользователь увидит, что
    # приложение не открылось, посмотрит лог и решит, что делать.
    script = f"""@echo off
chcp 65001 >nul
set "APP={current_exe}"
set "NEW={new_exe_path}"
set "LOG={log_path}"
set "PYINSTALLER_RESET_ENVIRONMENT=1"
timeout /t 2 /nobreak >nul
for /l %%i in (1,1,60) do (
  copy /Y "%NEW%" "%APP%" >nul 2>nul
  if not errorlevel 1 (
    start "" "%APP%"
    del "%NEW%" >nul 2>nul
    del "%~f0" >nul 2>nul
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)
echo [%date% %time%] Не удалось заменить приложение, перезапуск старого exe отключён во избежание цикла обновлений >> "%LOG%"
del "%~f0" >nul 2>nul
exit /b 1
"""
    with open(updater_path, "w", encoding="utf-8") as updater_file:
        updater_file.write(script)
    return updater_path

def create_windows_onedir_updater(update_zip_path, update_info, trusted_signers=None):
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Автообновление доступно только в собранной Windows-версии приложения")
    if os.name != "nt":
        raise RuntimeError("Автообновление сейчас поддерживает только Windows")

    validate_onedir_zip(update_zip_path)

    current_exe = os.path.abspath(sys.executable)
    app_dir = os.path.abspath(APP_DIR)
    updater_path = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_updater_{os.getpid()}.ps1")
    log_path = UPDATE_LOG_FILE
    extract_dir = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_update_extract_{os.getpid()}")
    process_id = os.getpid()
    entrypoint = normalize_text(update_info.get("entrypoint")) or APP_EXECUTABLE_NAME
    _, _, signer_certificate_sha256 = validate_update_manifest(
        update_info,
        trusted_signers=trusted_signers,
    )
    runtime_exclude_files = powershell_array(UPDATE_RUNTIME_EXCLUDE_FILES)
    runtime_preserve_files = powershell_array(UPDATE_RUNTIME_PRESERVE_FILES)
    runtime_exclude_dirs = powershell_array(UPDATE_RUNTIME_EXCLUDE_DIRS)

    shortcut_script = write_windows_shortcut_script(
        target_exe=os.path.join(app_dir, entrypoint),
        working_dir=app_dir,
        shortcut_path_expression=f"(Join-Path $Desktop '{APP_NAME}.lnk')",
    )

    script = f"""$ErrorActionPreference = 'Stop'
$AppDir = {powershell_single_quoted(app_dir)}
$ZipPath = {powershell_single_quoted(update_zip_path)}
$ExtractDir = {powershell_single_quoted(extract_dir)}
$LogPath = {powershell_single_quoted(log_path)}
$EntryPoint = {powershell_single_quoted(entrypoint)}
$ExpectedSignerCertificateSha256 = {powershell_single_quoted(signer_certificate_sha256)}
$ProcessIdToWait = {process_id}
$Desktop = [Environment]::GetFolderPath('Desktop')
$RuntimeExcludeFiles = {runtime_exclude_files}
$RuntimePreserveFiles = {runtime_preserve_files}
$RuntimeExcludeDirs = {runtime_exclude_dirs}
$ParentDir = [IO.Path]::GetDirectoryName($AppDir)
$UpdateStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$NewDir = Join-Path $ParentDir ("{APP_NAME}_new_" + $UpdateStamp)
$PreviousDir = Join-Path $ParentDir ("{APP_NAME}_previous_" + $UpdateStamp)

function Write-UpdateLog([string]$Message) {{
  $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  Add-Content -Path $LogPath -Value "[$stamp] $Message" -Encoding UTF8
}}

try {{
  Write-UpdateLog 'Старт onedir-обновления'
  while (Get-Process -Id $ProcessIdToWait -ErrorAction SilentlyContinue) {{
    Start-Sleep -Seconds 1
  }}

  if (Test-Path $ExtractDir) {{
    Remove-Item -LiteralPath $ExtractDir -Recurse -Force
  }}
  New-Item -ItemType Directory -Path $ExtractDir -Force | Out-Null
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractDir -Force

  $SourceDir = $ExtractDir
  $NestedDir = Join-Path $ExtractDir '{APP_NAME}'
  if (Test-Path (Join-Path $NestedDir $EntryPoint)) {{
    $SourceDir = $NestedDir
  }}
  if (-not (Test-Path (Join-Path $SourceDir $EntryPoint))) {{
    throw "В архиве обновления не найден $EntryPoint"
  }}
  $Signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $SourceDir $EntryPoint)
  if ($null -eq $Signature.SignerCertificate) {{
    throw "Authenticode-подпись обновления не содержит сертификат издателя"
  }}
  $Hasher = [System.Security.Cryptography.SHA256]::Create()
  try {{
    $SignerCertificateSha256 = -join ($Hasher.ComputeHash($Signature.SignerCertificate.RawData) | ForEach-Object {{ $_.ToString('x2') }})
  }} finally {{
    $Hasher.Dispose()
  }}
  if ($SignerCertificateSha256 -ne $ExpectedSignerCertificateSha256) {{
    throw "Authenticode-подпись создана недоверенным издателем"
  }}
  $Chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
  try {{
    $Chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $null = $Chain.Build($Signature.SignerCertificate)
    $ChainStatuses = @($Chain.ChainStatus | ForEach-Object {{ $_.Status.ToString() }} | Sort-Object -Unique)
  }} finally {{
    $Chain.Dispose()
  }}
  $AcceptedSignatureStatuses = @(
    [System.Management.Automation.SignatureStatus]::Valid,
    [System.Management.Automation.SignatureStatus]::NotTrusted,
    [System.Management.Automation.SignatureStatus]::UnknownError
  )
  if ($AcceptedSignatureStatuses -notcontains $Signature.Status) {{
    throw "Authenticode-подпись обновления недействительна: $($Signature.Status)"
  }}
  if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -and
      ($ChainStatuses.Count -ne 1 -or $ChainStatuses[0] -ne 'PartialChain')) {{
    throw "Authenticode-цепочка обновления недействительна: $($ChainStatuses -join ',')"
  }}

  if (Test-Path $NewDir) {{
    Remove-Item -LiteralPath $NewDir -Recurse -Force
  }}
  New-Item -ItemType Directory -Path $NewDir -Force | Out-Null
  Write-UpdateLog ("Runtime files excluded from update copy: " + ($RuntimeExcludeFiles -join ', '))
  robocopy $SourceDir $NewDir /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NP /XF $RuntimeExcludeFiles /XD $RuntimeExcludeDirs | Out-Null
  if ($LASTEXITCODE -gt 7) {{
    throw "robocopy failed with exit code $LASTEXITCODE"
  }}

  if (Test-Path $AppDir) {{
    foreach ($Name in $RuntimePreserveFiles) {{
      Get-ChildItem -Path $AppDir -Force -File -Filter $Name -ErrorAction SilentlyContinue |
        Copy-Item -Destination $NewDir -Force
    }}
    foreach ($DirName in $RuntimeExcludeDirs) {{
      $RuntimeDirSource = Join-Path $AppDir $DirName
      $RuntimeDirTarget = Join-Path $NewDir $DirName
      if (Test-Path $RuntimeDirSource) {{
        if (Test-Path $RuntimeDirTarget) {{
          Remove-Item -LiteralPath $RuntimeDirTarget -Recurse -Force
        }}
        Copy-Item -LiteralPath $RuntimeDirSource -Destination $RuntimeDirTarget -Recurse -Force
      }}
    }}
    Move-Item -LiteralPath $AppDir -Destination $PreviousDir -Force
  }}
  Move-Item -LiteralPath $NewDir -Destination $AppDir -Force

{shortcut_script}

  $NewExe = Join-Path $AppDir $EntryPoint
  Write-UpdateLog "Обновление установлено: $NewExe"
  Start-Process -FilePath $NewExe -WorkingDirectory $AppDir
  Write-UpdateLog "Previous app dir retained for health-confirmed/manual rollback: $PreviousDir"
  Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $ExtractDir -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}} catch {{
  # ВАЖНО: при ошибке НЕ перезапускаем старый exe. Иначе старый exe снова
  # обнаружит «нужно обновиться», снова запустит этот же updater, и
  # получится бесконечный цикл «приложение само открывается после закрытия».
  # Пользователь увидит, что приложение не открылось, посмотрит лог и
  # решит, что делать.
  Write-UpdateLog ("Ошибка onedir-обновления: " + $_.Exception.Message)
  if (Test-Path $PreviousDir) {{
    try {{
      Write-UpdateLog "Пробую восстановить previous app dir после неудачного обновления."
      if (Test-Path $AppDir) {{
        $FailedDir = Join-Path $ParentDir ("{APP_NAME}_failed_" + (Get-Date -Format 'yyyyMMdd_HHmmss'))
        Move-Item -LiteralPath $AppDir -Destination $FailedDir -Force
      }}
      Move-Item -LiteralPath $PreviousDir -Destination $AppDir -Force
      Write-UpdateLog "Previous app dir restored after failed update."
    }} catch {{
      Write-UpdateLog ("Не удалось восстановить previous app dir: " + $_.Exception.Message)
    }}
  }}
  Remove-Item -LiteralPath $NewDir -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $ExtractDir -Recurse -Force -ErrorAction SilentlyContinue
  Write-UpdateLog "Перезапуск старого exe отключён во избежание цикла обновлений."
  Write-UpdateLog "Безопасное действие: установите свежий Windows-архив вручную и запускайте только новый TakSklad.exe."
  exit 1
}}
"""
    with open(updater_path, "w", encoding="utf-8-sig") as updater_file:
        updater_file.write(script)
    return updater_path

def prepare_update_installer(update_info):
    downloaded_path = download_update_file(update_info)
    package_type = detect_update_package_type(update_info, downloaded_path)
    if package_type in ("onedir", "onedir_zip", "zip"):
        return create_windows_onedir_updater(downloaded_path, update_info)
    return create_windows_exe_updater(downloaded_path)

def maybe_rename_windows_executable():
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return False

    current_exe = os.path.abspath(sys.executable)
    target_exe = os.path.join(os.path.dirname(current_exe), APP_EXECUTABLE_NAME)
    if os.path.basename(current_exe).lower() == APP_EXECUTABLE_NAME.lower():
        return False

    updater_path = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_rename_{os.getpid()}.bat")
    log_path = UPDATE_LOG_FILE
    # ВАЖНО: при ошибке копирования НЕЛЬЗЯ перезапускать старый exe.
    # У старого exe имя отличается от APP_EXECUTABLE_NAME, поэтому он
    # снова войдёт в maybe_rename_windows_executable, снова создаст .bat,
    # копия снова упадёт — и получится бесконечный цикл «приложение само
    # открывается после закрытия». Пишем в лог и выходим.
    script = f"""@echo off
chcp 65001 >nul
set "OLD={current_exe}"
set "NEW={target_exe}"
set "LOG={log_path}"
set "PYINSTALLER_RESET_ENVIRONMENT=1"
timeout /t 1 /nobreak >nul
copy /Y "%OLD%" "%NEW%" >nul 2>nul
if errorlevel 1 (
  echo [%date% %time%] Не удалось создать "%NEW%", перезапуск старого exe отключён во избежание цикла >> "%LOG%"
  del "%~f0" >nul 2>nul
  exit /b 1
)
start "" "%NEW%"
timeout /t 3 /nobreak >nul
del "%OLD%" >nul 2>nul
del "%~f0" >nul 2>nul
exit /b 0
"""
    with open(updater_path, "w", encoding="utf-8") as updater_file:
        updater_file.write(script)

    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    subprocess.Popen(["cmd", "/c", updater_path], creationflags=creationflags)
    return True
