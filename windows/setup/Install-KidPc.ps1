#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Turns a fresh Windows 11 PC into a kid PC: standard account, kid shell instead of Explorer,
  admin helper service, Chrome policies, lockdown. Run once per PC from an admin account.

.EXAMPLE
  .\Install-KidPc.ps1 -KidUser adam -KidName Adam -HubUrl http://truenas.local:8080 `
     -DeviceToken (Read-Host "device token") -ExtensionToken (Read-Host "extension token") `
     -BuildDir ..\publish

.NOTES
  Order on a new PC:
   1. Set up Windows with a parent ADMIN account (local account; see docs\SETUP.md for skipping the Microsoft account).
   2. Install Steam, Battle.net and Chrome as that admin.
   3. Run this script. It creates the kid account if missing.
   4. Sign in as the kid once (creates his profile), sign out, run this script again with -ShellOnly
      so the shell can be written into his registry hive.
   5. Set up automatic sign-in with Sysinternals Autologon (stores the password as an LSA secret).
#>
param(
  [Parameter(Mandatory)] [string] $KidUser,
  [Parameter(Mandatory)] [string] $KidName,
  [string] $HubUrl = "http://truenas.local:8080",
  [string] $DeviceToken,
  [string] $ExtensionToken,
  [string] $BuildDir = "$PSScriptRoot\..\publish",
  [string] $InstallDir = "C:\Program Files\KidsPc",
  [ValidateSet("allowlist", "filtered")] [string] $WebMode = "filtered",
  [string] $ExtensionId = "",          # set once the reporting extension has an id (see chrome\README.md)
  [switch] $ShellOnly
)
$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if (-not $ShellOnly) {
  # ------------------------------------------------------------------ kid account
  if (-not (Get-LocalUser -Name $KidUser -ErrorAction SilentlyContinue)) {
    Step "Creating standard account '$KidUser'"
    $pw = Read-Host "Password for $KidName's account (used by Autologon)" -AsSecureString
    New-LocalUser -Name $KidUser -FullName $KidName -Password $pw -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    Add-LocalGroupMember -Group "Users" -Member $KidUser
  }
  if ((Get-LocalGroupMember Administrators).Name -match "\\$KidUser$") { throw "$KidUser must NOT be an administrator." }

  # ------------------------------------------------------------------ binaries
  Step "Copying shell and helper to $InstallDir"
  New-Item -ItemType Directory -Force -Path "$InstallDir\Shell", "$InstallDir\Helper" | Out-Null
  Copy-Item -Recurse -Force "$BuildDir\Shell\*" "$InstallDir\Shell\"
  Copy-Item -Recurse -Force "$BuildDir\Helper\*" "$InstallDir\Helper\"

  # ------------------------------------------------------------------ device.json (admin-only)
  Step "Writing device config"
  $dataDir = "$env:ProgramData\KidsPc"
  New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
  @{ hubUrl = $HubUrl; deviceToken = $DeviceToken; kidUser = $KidUser } | ConvertTo-Json | Set-Content "$dataDir\device.json" -Encoding UTF8
  icacls $dataDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

  # ------------------------------------------------------------------ helper service
  Step "Installing KidsPcHelper service (LocalSystem)"
  $svc = Get-Service KidsPcHelper -ErrorAction SilentlyContinue
  if ($svc) { Stop-Service KidsPcHelper -Force; sc.exe delete KidsPcHelper | Out-Null; Start-Sleep 2 }
  New-Service -Name KidsPcHelper -DisplayName "Kids PC Helper" -BinaryPathName "`"$InstallDir\Helper\KidsPcHelper.exe`"" `
              -StartupType Automatic -Description "Parental controls: rules, time limits, installs." | Out-Null
  sc.exe failure KidsPcHelper reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
  Start-Service KidsPcHelper

  # ------------------------------------------------------------------ machine-wide lockdown
  Step "Machine policies"
  $sys = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
  Set-ItemProperty $sys HideFastUserSwitching 1 -Type DWord
  # Keep the kid out of Windows' own Microsoft-account and consumer nags.
  New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" | Out-Null
  Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" DisableWindowsConsumerFeatures 1 -Type DWord

  & "$PSScriptRoot\..\..\chrome\policy\Set-ChromePolicy.ps1" -HubUrl $HubUrl -ExtensionToken $ExtensionToken -ExtensionId $ExtensionId -WebMode $WebMode
}

# ------------------------------------------------------------------ per-user shell + lockdown (kid hive)
$profilePath = (Get-CimInstance Win32_UserProfile | Where-Object { $_.LocalPath -like "*\$KidUser" }).LocalPath
if (-not $profilePath) {
  Write-Warning "$KidUser has no profile yet. Sign in as $KidName once, sign out, then run: .\Install-KidPc.ps1 -KidUser $KidUser -KidName $KidName -ShellOnly"
  return
}
Step "Setting the kid shell in $KidUser's registry"
$sid = (Get-LocalUser $KidUser).SID.Value
$loaded = Test-Path "Registry::HKEY_USERS\$sid"
$hive = if ($loaded) { "HKEY_USERS\$sid" } else { reg load "HKU\KidsPcTmp" "$profilePath\NTUSER.DAT" | Out-Null; "HKEY_USERS\KidsPcTmp" }
try {
  $h = "Registry::$hive"
  # Per-user shell replacement: Winlogon starts this instead of explorer.exe, for this account only.
  New-Item -Force "$h\Software\Microsoft\Windows NT\CurrentVersion\Winlogon" | Out-Null
  Set-ItemProperty "$h\Software\Microsoft\Windows NT\CurrentVersion\Winlogon" Shell "$InstallDir\Shell\KidsPcShell.exe"

  $pol = "$h\Software\Microsoft\Windows\CurrentVersion\Policies"
  New-Item -Force "$pol\System", "$pol\Explorer" | Out-Null
  Set-ItemProperty "$pol\System" DisableTaskMgr 1 -Type DWord           # Ctrl+Alt+Del > Task Manager
  Set-ItemProperty "$pol\System" DisableRegistryTools 1 -Type DWord
  Set-ItemProperty "$pol\System" DisableChangePassword 1 -Type DWord
  Set-ItemProperty "$pol\Explorer" NoRun 1 -Type DWord
  Set-ItemProperty "$pol\Explorer" NoControlPanel 1 -Type DWord
  New-Item -Force "$h\Software\Policies\Microsoft\Windows\System" | Out-Null
  Set-ItemProperty "$h\Software\Policies\Microsoft\Windows\System" DisableCMD 1 -Type DWord   # cmd.exe and .bat
  # TODO: AppLocker / WDAC rules so PowerShell and arbitrary downloaded .exe files cannot run for this user.
  #       Check whether AppLocker enforces on your Windows edition; App Control for Business (WDAC) works on Pro.
}
finally {
  if (-not $loaded) { [gc]::Collect(); reg unload "HKU\KidsPcTmp" | Out-Null }
}

Step "Done. Next: run Sysinternals Autologon for $KidUser, then reboot."
