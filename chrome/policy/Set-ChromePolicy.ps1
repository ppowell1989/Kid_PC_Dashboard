#Requires -RunAsAdministrator
<#
  Static Chrome lockdown for the kid PCs (HKLM, applies to every Chrome profile on the machine).
  The admin helper rewrites URLBlocklist/URLAllowlist on its own when the hub's web_mode or
  allowed sites change, so this script only needs to run once.
  Reference: https://chromeenterprise.google/policies/
#>
param(
  [string] $HubUrl = "http://truenas.local:8080",
  [string] $ExtensionToken = "",
  [string] $ExtensionId = "",           # 32-char id of the reporting extension (see chrome\README.md)
  [string] $ExtensionUpdateUrl = "https://clients2.google.com/service/update2/crx",
  [ValidateSet("allowlist", "filtered")] [string] $WebMode = "filtered"
)
$ErrorActionPreference = "Stop"
$c = "HKLM:\SOFTWARE\Policies\Google\Chrome"
New-Item -Force $c | Out-Null

function Dword($name, $v) { Set-ItemProperty $c $name $v -Type DWord }
function Str($name, $v)   { Set-ItemProperty $c $name $v -Type String }
function List($name, [string[]] $values) {
  $k = "$c\$name"; Remove-Item -Recurse -Force $k -ErrorAction SilentlyContinue; New-Item -Force $k | Out-Null
  for ($i = 0; $i -lt $values.Count; $i++) { Set-ItemProperty $k ($i + 1) $values[$i] -Type String }
}

Dword IncognitoModeAvailability 1        # incognito off
Dword BrowserGuestModeEnabled 0
Dword BrowserAddPersonEnabled 0
Dword BrowserSignin 0                    # no signing in to Chrome (keeps history local and visible)
Dword SyncDisabled 1
Dword ForceGoogleSafeSearch 1
Dword ForceYouTubeRestrict 2             # 1 = moderate, 2 = strict. GUESS: strict for under-13s
Dword SafeBrowsingProtectionLevel 1
Dword DeveloperToolsAvailability 2       # dev tools off
Dword AllowDeletingBrowserHistory 0
Dword SavingBrowserHistoryDisabled 0
Dword PasswordManagerEnabled 0
Dword DownloadRestrictions 3             # block all downloads. GUESS: relax to 1 (dangerous only) if he needs downloads
Str   DnsOverHttpsMode "off"             # so AdGuard Home sees (and filters) every lookup
Dword BuiltInDnsClientEnabled 0
Dword DefaultPopupsSetting 2

# Extensions: block everything except the reporting extension.
List ExtensionInstallBlocklist @("*")
if ($ExtensionId) {
  List ExtensionInstallAllowlist @($ExtensionId)
  List ExtensionInstallForcelist @("$ExtensionId;$ExtensionUpdateUrl")
  # Managed storage for the extension (read with chrome.storage.managed, see extension\schema.json)
  $e = "HKLM:\SOFTWARE\Policies\Google\Chrome\3rdparty\extensions\$ExtensionId\policy"
  New-Item -Force $e | Out-Null
  Set-ItemProperty $e hubUrl $HubUrl -Type String
  Set-ItemProperty $e token $ExtensionToken -Type String
} else {
  Write-Warning "No -ExtensionId given: Chrome is locked down, but page-level reporting is off until the extension is installed."
}

if ($WebMode -eq "allowlist") {
  List URLBlocklist @("*")
  List URLAllowlist @("chrome://*", "google.com", "wikipedia.org")
} else {
  Remove-Item -Recurse -Force "$c\URLBlocklist", "$c\URLAllowlist" -ErrorAction SilentlyContinue
}
Write-Host "Chrome policies written. Check them at chrome://policy"
