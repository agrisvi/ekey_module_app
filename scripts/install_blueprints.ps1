# ekey Blueprint Installation Script for Windows
# This script copies blueprints to the correct Home Assistant directories
#
# Only AUTOMATION blueprints remain. The two script blueprints (enrol, delete) are
# gone: enrolment and deletion now happen in the ekey panel in the sidebar, which
# shows live progress and can assign a fingerprint enrolled on the device itself.

Write-Host "ekey Blueprint Installer" -ForegroundColor Blue
Write-Host "================================" -ForegroundColor Blue
Write-Host ""

# Detect Home Assistant config directory
$ConfigDir = $null

if (Test-Path "\\wsl$\homeassistant\config") {
    $ConfigDir = "\\wsl$\homeassistant\config"
    Write-Host "Detected Home Assistant on WSL"
} elseif (Test-Path "$env:APPDATA\.homeassistant") {
    $ConfigDir = "$env:APPDATA\.homeassistant"
    Write-Host "Detected Home Assistant Core installation"
} else {
    Write-Host "Could not auto-detect Home Assistant config directory."
    Write-Host "Common locations:"
    Write-Host "  - \\wsl`$\homeassistant\config (WSL)"
    Write-Host "  - \\HOMEASSISTANT\config (Samba/Network share)"
    Write-Host "  - Z:\config (Mapped network drive)"
    Write-Host ""
    $ConfigDir = Read-Host "Enter config directory path"
}

if (-not (Test-Path $ConfigDir)) {
    Write-Host "Error: Config directory not found: $ConfigDir" -ForegroundColor Red
    exit 1
}

Write-Host "Using config directory: $ConfigDir" -ForegroundColor Green
Write-Host ""

# Find source blueprints. This script lives in scripts/ at the repository root; the
# blueprints ship inside the integration, so the path goes back up and across. Only a
# repository clone has both — HACS installs custom_components\ekey_ha_app\ and nothing
# else, so a HACS user never sees this script and imports the YAML by hand instead.
$ScriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceDir = Join-Path $ScriptPath "..\custom_components\ekey_ha_app\blueprints"

if (-not (Test-Path $SourceDir)) {
    Write-Host "Error: Blueprints directory not found at $SourceDir" -ForegroundColor Red
    exit 1
}

$AutoDest = Join-Path $ConfigDir "blueprints\automation\ekey"

Write-Host "Creating blueprint directory..."
New-Item -ItemType Directory -Path $AutoDest -Force | Out-Null

# A loop rather than one Copy-Item per name, and a missing file is an ERROR. The
# previous version copied a blueprint that did not exist
# (door_unlock_on_match.yaml) and silently skipped one that did.
$Blueprints = @("toggle_relay_on_granted.yaml", "welcome_notification.yaml", "access_notification_list.yaml")

Write-Host ""
Write-Host "Copying automation blueprints..."
$failed = $false
foreach ($bp in $Blueprints) {
    $src = Join-Path $SourceDir $bp
    if (-not (Test-Path $src)) {
        Write-Host "X $bp - not found in $SourceDir" -ForegroundColor Red
        $failed = $true
        continue
    }
    try {
        Copy-Item $src "$AutoDest\" -Force -ErrorAction Stop
        Write-Host "OK $bp" -ForegroundColor Green
    } catch {
        Write-Host "X $bp - $($_.Exception.Message)" -ForegroundColor Red
        $failed = $true
    }
}

if ($failed) {
    Write-Host ""
    Write-Host "Installation incomplete." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Older copies elsewhere — same check as the shell installer, same reason. Home
# Assistant identifies a blueprint by its PATH, so an automation keeps using the copy
# it was created from and overwriting this folder does not touch it. The symptom is two
# blueprints with almost the same name and an automation whose entity picker is empty
# because it still refers to select.*_enrolled_fingerprints, which no longer exists.
$AutoRoot = Join-Path $ConfigDir "blueprints\automation"
if (Test-Path $AutoRoot) {
    $stale = Get-ChildItem -Path $AutoRoot -Filter *.yaml -Recurse -File -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -notlike "$AutoDest\*" } |
             Where-Object { Select-String -Path $_.FullName -Pattern 'ekey' -Quiet -CaseSensitive:$false }

    if ($stale) {
        Write-Host ""
        Write-Host "Other ekey blueprints found outside ${AutoDest}:" -ForegroundColor Blue
        foreach ($f in $stale) {
            if (Select-String -Path $f.FullName -Pattern 'enrolled_fingerprints' -Quiet) {
                Write-Host "  ! $($f.FullName)  (uses the removed select entity)" -ForegroundColor Red
            } else {
                Write-Host "  - $($f.FullName)"
            }
        }
        Write-Host ""
        Write-Host "  An automation created from one of these still uses THAT file, not the one"
        Write-Host "  just installed. For each: open the automation, recreate it from the ekey"
        Write-Host "  blueprint in $AutoDest, then delete the old file."
    }
}

Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Go to Developer Tools -> YAML -> Reload Automations"
Write-Host "2. Go to Settings -> Automations & scenes -> Blueprints"
Write-Host "3. Click 'Create automation' on an ekey blueprint"
Write-Host ""
Write-Host "Users and fingerprints are managed in the ekey panel in the sidebar,"
Write-Host "not by a blueprint. See docs/QUICKSTART.md."
Write-Host ""
Read-Host "Press Enter to exit"
