#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Turns a fresh Windows 11 PC into a kid PC in one run: standard account (with its profile made
  up front, so no sign-in/sign-out dance), kid shell instead of Explorer, admin helper service,
  Chrome policies, lockdown, automatic sign-in, then a check that it all took.

.DESCRIPTION
  The easy way is not to run this by hand: on the dashboard, the kid's card, "Set up a new PC" gives
  one line to paste into an admin PowerShell. That line fetches the tokens and the newest build from
  the hub and runs this script with the right arguments.

.EXAMPLE
  .\Install-KidPc.ps1 -KidUser adam -KidName Adam -HubUrl http://192.168.4.54:8088 `
     -DeviceToken <token> -ExtensionToken <token> -AutoLogon -Restart
  # -BuildDir defaults to the downloaded CI bundle this script sits in

.NOTES
  Run it from the parent's ADMIN account (a local account; see docs\CHRISTMAS.md).
  Re-running is safe: it updates the binaries and settings and keeps the hub address and tokens.
  For CI and scripted runs, the kid's password can come from $env:KIDSPC_KID_PASSWORD.
#>
param(
  [Parameter(Mandatory)] [string] $KidUser,
  [Parameter(Mandatory)] [string] $KidName,
  [string] $HubUrl = "http://truenas.local:8080",
  [string] $DeviceToken,
  [string] $ExtensionToken,
  [string] $BuildDir = "",              # default: the bundle folder this script sits in (CI download), else ..\publish\win-x64
  [string] $InstallDir = "C:\Program Files\KidsPc",
  [ValidateSet("allowlist", "filtered")] [string] $WebMode = "filtered",
  [string] $ExtensionId = "",          # set once the reporting extension has an id (see chrome\README.md)
  [securestring] $KidPassword,         # asked for when needed and not given
  [switch] $AutoLogon,                 # sign the kid in by himself when the PC starts
  [switch] $Restart,                   # restart at the end so the kid shell comes up
  [int] $AppWaitMinutes = 10,          # how long to wait for the helper to install Steam and Chrome (0: don't wait)
  [switch] $ShellOnly                  # older two-step setup: only write the shell into the kid's registry
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
if (-not $BuildDir) {
  $BuildDir = if (Test-Path "$PSScriptRoot\..\Shell") { "$PSScriptRoot\.." } else { "$PSScriptRoot\..\publish\win-x64" }
}
$chromeScript = @("$PSScriptRoot\..\chrome\policy\Set-ChromePolicy.ps1", "$PSScriptRoot\..\..\chrome\policy\Set-ChromePolicy.ps1") |
  Where-Object { Test-Path $_ } | Select-Object -First 1
$dataDir = "$env:ProgramData\KidsPc"
$winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Plain([securestring] $s) {
  $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
  try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Test-LocalPassword([string] $user, [string] $pw) {
  Add-Type -AssemblyName System.DirectoryServices.AccountManagement
  $ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext([System.DirectoryServices.AccountManagement.ContextType]::Machine)
  try { $ctx.ValidateCredentials($user, $pw) } finally { $ctx.Dispose() }
}

# Windows API calls PowerShell has no command for: make a user's profile without signing in, and keep
# the automatic sign-in password where Windows wants it (an LSA secret, not readable from the registry).
if (-not ("KidsPcWin" -as [type])) { Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class KidsPcWin {
  [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
  static extern int CreateProfile(string sid, string user, StringBuilder path, uint len);
  public static string MakeProfile(string sid, string user) {
    var sb = new StringBuilder(260);
    int hr = CreateProfile(sid, user, sb, 260);
    if (hr == unchecked((int)0x800700B7)) return "";   // already exists
    if (hr != 0) throw new System.ComponentModel.Win32Exception(hr & 0xFFFF);
    return sb.ToString();
  }

  [StructLayout(LayoutKind.Sequential)] struct LsaString { public ushort Length, MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)] struct LsaAttrs { public int Length; public IntPtr Root, Name; public uint Attributes; public IntPtr Sd, Qos; }
  [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref LsaAttrs attrs, uint access, out IntPtr policy);
  [DllImport("advapi32.dll")] static extern uint LsaStorePrivateData(IntPtr policy, ref LsaString key, ref LsaString data);
  [DllImport("advapi32.dll")] static extern uint LsaRetrievePrivateData(IntPtr policy, ref LsaString key, out IntPtr data);
  [DllImport("advapi32.dll")] static extern uint LsaFreeMemory(IntPtr p);
  [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr policy);
  [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint status);
  static LsaString Str(string s) {
    return new LsaString { Buffer = Marshal.StringToHGlobalUni(s), Length = (ushort)(s.Length * 2), MaximumLength = (ushort)(s.Length * 2 + 2) };
  }
  static IntPtr Open() {
    var a = new LsaAttrs(); IntPtr p;
    uint st = LsaOpenPolicy(IntPtr.Zero, ref a, 0x00F0FFF, out p);
    if (st != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(st));
    return p;
  }
  public static void StoreSecret(string key, string value) {
    IntPtr p = Open(); var k = Str(key); var d = Str(value);
    try {
      uint st = LsaStorePrivateData(p, ref k, ref d);
      if (st != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(st));
    } finally { Marshal.FreeHGlobal(k.Buffer); Marshal.FreeHGlobal(d.Buffer); LsaClose(p); }
  }
  public static bool HasSecret(string key) {
    IntPtr p = Open(); var k = Str(key); IntPtr d;
    try {
      if (LsaRetrievePrivateData(p, ref k, out d) != 0 || d == IntPtr.Zero) return false;
      var s = (LsaString)Marshal.PtrToStructure(d, typeof(LsaString));
      LsaFreeMemory(d);
      return s.Length > 0;
    } finally { Marshal.FreeHGlobal(k.Buffer); LsaClose(p); }
  }
}
"@ }

# ------------------------------------------------------------------ kid account
$account = Get-LocalUser -Name $KidUser -ErrorAction SilentlyContinue
$pw = $null
if ($KidPassword) { $pw = Plain $KidPassword }
elseif ($env:KIDSPC_KID_PASSWORD) { $pw = $env:KIDSPC_KID_PASSWORD }

if (-not $ShellOnly) {
  if (-not $account) {
    Step "Creating $KidName's Windows account '$KidUser' (standard, not admin)"
    while (-not $pw) {
      $a = Plain (Read-Host "  Choose a password for $KidName's account (he types it only if he signs out)" -AsSecureString)
      $b = Plain (Read-Host "  Type it again" -AsSecureString)
      if (-not $a) { Write-Host "  It can't be empty." -ForegroundColor Yellow }
      elseif ($a -ne $b) { Write-Host "  Those didn't match, try again." -ForegroundColor Yellow }
      else { $pw = $a }
    }
    $account = New-LocalUser -Name $KidUser -FullName $KidName -Password (ConvertTo-SecureString $pw -AsPlainText -Force) `
      -PasswordNeverExpires -UserMayNotChangePassword
    Add-LocalGroupMember -SID "S-1-5-32-545" -Member $KidUser -ErrorAction SilentlyContinue   # Users
  }
  elseif ($AutoLogon) {
    # Automatic sign-in needs the account's real password; a typo here would leave the PC at the sign-in screen.
    while (-not $pw -or -not (Test-LocalPassword $KidUser $pw)) {
      if ($pw) { Write-Host "  That isn't $KidUser's password, try again." -ForegroundColor Yellow }
      $pw = Plain (Read-Host "  $KidName's account already exists. Its password (for automatic sign-in)" -AsSecureString)
    }
  }
  $adminGroup = (Get-LocalGroup -SID "S-1-5-32-544").Name
  if ((net localgroup $adminGroup) -contains $KidUser) { throw "$KidUser must NOT be an administrator. Remove it from $adminGroup and run this again." }
}
if (-not $account) { throw "There is no account named $KidUser." }
$sid = $account.SID.Value

# ------------------------------------------------------------------ kid profile, made now instead of at his first sign-in
$kidProfile = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" -ErrorAction SilentlyContinue
if (-not $kidProfile) {
  Step "Making $KidName's profile folder"
  [KidsPcWin]::MakeProfile($sid, $KidUser) | Out-Null
  $kidProfile = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'"
}
$profilePath = $kidProfile.LocalPath

if (-not $ShellOnly) {
  # ------------------------------------------------------------------ binaries
  # Stop a previously installed helper first, or its files are locked and can't be replaced.
  if (Get-Service KidsPcHelper -ErrorAction SilentlyContinue) { Stop-Service KidsPcHelper -Force; Start-Sleep 2 }
  Get-Process KidsPcShell -ErrorAction SilentlyContinue | Stop-Process -Force

  Step "Copying shell and helper to $InstallDir"
  New-Item -ItemType Directory -Force -Path "$InstallDir\Shell", "$InstallDir\Helper" | Out-Null
  Copy-Item -Recurse -Force "$BuildDir\Shell\*" "$InstallDir\Shell\"
  Copy-Item -Recurse -Force "$BuildDir\Helper\*" "$InstallDir\Helper\"

  # ------------------------------------------------------------------ device.json (admin-only)
  Step "Writing device config"
  New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
  # A build installed by hand replaces whatever the updater had downloaded and not yet installed.
  Remove-Item -Recurse -Force "$dataDir\staged", "$dataDir\update-forced.txt" -ErrorAction SilentlyContinue
  # Re-running to update: keep the hub address and tokens from the last install unless new ones are given.
  if (-not $DeviceToken -and (Test-Path "$dataDir\device.json")) {
    $prev = Get-Content "$dataDir\device.json" -Raw | ConvertFrom-Json
    $DeviceToken = $prev.deviceToken
    if (-not $PSBoundParameters.ContainsKey('HubUrl') -and $prev.hubUrl) { $HubUrl = $prev.hubUrl }
    if (-not $ExtensionToken -and $prev.extensionToken) { $ExtensionToken = $prev.extensionToken }
    Write-Host "    reusing hub $HubUrl and tokens from the last install"
  }
  if (-not $DeviceToken) { throw "No -DeviceToken given and no previous install to reuse it from. Use the dashboard's Set up a new PC line instead." }
  @{ hubUrl = $HubUrl; deviceToken = $DeviceToken; extensionToken = $ExtensionToken; kidUser = $KidUser } | ConvertTo-Json | Set-Content "$dataDir\device.json" -Encoding UTF8
  icacls $dataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null   # SYSTEM, Administrators

  # ------------------------------------------------------------------ helper service
  Step "Installing KidsPcHelper service (LocalSystem)"
  $svc = Get-Service KidsPcHelper -ErrorAction SilentlyContinue
  if ($svc) { Stop-Service KidsPcHelper -Force; sc.exe delete KidsPcHelper | Out-Null; Start-Sleep 2 }
  New-Service -Name KidsPcHelper -DisplayName "Kids PC Helper" -BinaryPathName "`"$InstallDir\Helper\KidsPcHelper.exe`"" `
              -StartupType Automatic -Description "Parental controls: rules, time limits, installs." | Out-Null
  sc.exe failure KidsPcHelper reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
  Start-Service KidsPcHelper

  # ------------------------------------------------------------------ machine-wide settings
  Step "Machine policies"
  $sys = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
  Set-ItemProperty $sys HideFastUserSwitching 1 -Type DWord
  Set-ItemProperty $sys EnableFirstLogonAnimation 0 -Type DWord        # no "Hi / getting things ready" on his first sign-in
  New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" | Out-Null
  Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" DisablePrivacyExperience 1 -Type DWord   # no privacy-settings screen for a new account
  # Keep the kid out of Windows' own Microsoft-account and consumer nags.
  New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" | Out-Null
  Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" DisableWindowsConsumerFeatures 1 -Type DWord
  # Windows Update must not restart the PC under him in the middle of a game.
  New-Item -Force "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" | Out-Null
  Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" NoAutoRebootWithLoggedOnUsers 1 -Type DWord
  # Waking from sleep goes straight back to his screen instead of the password box.
  powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 2>$null | Out-Null
  powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 2>$null | Out-Null

  & $chromeScript -HubUrl $HubUrl -ExtensionToken $ExtensionToken -ExtensionId $ExtensionId -WebMode $WebMode

  # ------------------------------------------------------------------ automatic sign-in
  if ($AutoLogon) {
    Step "Signing $KidName in automatically when the PC starts"
    if (-not $pw) { throw "Automatic sign-in needs $KidUser's password." }
    [KidsPcWin]::StoreSecret("DefaultPassword", $pw)
    Set-ItemProperty $winlogon AutoAdminLogon "1" -Type String
    Set-ItemProperty $winlogon DefaultUserName $KidUser -Type String
    Set-ItemProperty $winlogon DefaultDomainName $env:COMPUTERNAME -Type String
    # Never a plain-text copy, never a limited count, and no ForceAutoLogon: after he signs out a parent can still sign in.
    foreach ($v in "DefaultPassword", "AutoLogonCount", "ForceAutoLogon") { Remove-ItemProperty $winlogon $v -ErrorAction SilentlyContinue }
    # Windows 11 hides password sign-in when "Windows Hello only" is on; automatic sign-in needs it.
    New-Item -Force "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" | Out-Null
    Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" DevicePasswordLessBuildVersion 0 -Type DWord
  }
}

# ------------------------------------------------------------------ per-user shell + lockdown (kid hive)
Step "Setting the kid shell in $KidUser's registry"
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
  # No "let's finish setting up your device" pages on his account.
  New-Item -Force "$h\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement" | Out-Null
  Set-ItemProperty "$h\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement" ScoobeSystemSettingEnabled 0 -Type DWord
  # TODO: AppLocker / WDAC rules so PowerShell and arbitrary downloaded .exe files cannot run for this user.
}
finally {
  if (-not $loaded) { [gc]::Collect(); Start-Sleep 1; reg unload "HKU\KidsPcTmp" | Out-Null }
}
if ($ShellOnly) { Step "Done."; return }

# ------------------------------------------------------------------ check it all took
Step "Checking"
$fails = 0
function Check($name, [bool] $ok, $detail = "") {
  if ($ok) { Write-Host "    OK    $name" -ForegroundColor Green }
  else { Write-Host "    FAIL  $name $detail" -ForegroundColor Red; $script:fails++ }
}
Check "$KidUser is a standard account" (-not ((net localgroup $adminGroup) -contains $KidUser))
Check "$KidName's profile exists" (Test-Path "$profilePath\NTUSER.DAT")
Check "helper service is running" ((Get-Service KidsPcHelper).Status -eq "Running")
$rules = $null
try { $rules = Invoke-RestMethod "$HubUrl/api/device/rules" -Headers @{ Authorization = "Bearer $DeviceToken" } -TimeoutSec 15 } catch { }
Check "hub $HubUrl knows this PC" ($null -ne $rules) "(check the hub address and that the hub is running)"
if ($AutoLogon) {
  $wl = Get-ItemProperty $winlogon
  Check "automatic sign-in as $KidUser" ($wl.AutoAdminLogon -eq "1" -and $wl.DefaultUserName -eq $KidUser -and [KidsPcWin]::HasSecret("DefaultPassword"))
}

if ($AppWaitMinutes -gt 0) {
  # The helper installs Steam and Chrome by itself (as SYSTEM) once it has talked to the hub.
  $apps = [ordered]@{ Steam = "${env:ProgramFiles(x86)}\Steam\steam.exe"; Chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" }
  $until = (Get-Date).AddMinutes($AppWaitMinutes)
  $waiting = @($apps.Keys | Where-Object { -not (Test-Path $apps[$_]) })
  if ($waiting) { Write-Host "    ...waiting for the helper to install $($waiting -join ' and ') (up to $AppWaitMinutes minutes)" }
  while ((Get-Date) -lt $until -and @($apps.Keys | Where-Object { -not (Test-Path $apps[$_]) }).Count) { Start-Sleep 10 }
  foreach ($n in $apps.Keys) { Check "$n installed" (Test-Path $apps[$n]) "(the helper keeps trying; Retry is on the dashboard)" }
}

Write-Host ""
if ($fails) {
  Write-Host "Setup finished with $fails problem(s) above. Fix them and run the same line again; it is safe to repeat." -ForegroundColor Yellow
  $global:LASTEXITCODE = 1
  return
}
Write-Host "$KidName's PC is ready." -ForegroundColor Green
if ($Restart) {
  Write-Host "Restarting in 15 seconds so $KidName's screen comes up. Press Ctrl+C to stay here."
  Start-Sleep 15
  Restart-Computer -Force
}
else { Write-Host "Restart the PC and $KidName's screen comes up by itself." }
