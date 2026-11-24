<#
  New-PC setup, end to end, on a clean Windows machine (CI's windows-latest runner, which has never seen
  a kid PC): starts a hub with the build on it, makes a setup code the way the dashboard does, then runs
  the exact line a parent pastes, in Windows PowerShell 5.1 like the Terminal on a new PC.
  Checks the result the way Windows will use it at the next start. Runs the line twice: re-running
  must be safe, because that is the advice when anything goes wrong.
  Usage (admin, from the repo root): .\windows\tests\FreshPc.ps1 -BuildDir bundle
#>
param([Parameter(Mandatory)] [string] $BuildDir, [int] $Port = 8766, [string] $KidUser = "barrett")
$ErrorActionPreference = "Stop"
$fail = 0
function Check($name, $cond, $detail = "") {
  if ($cond) { Write-Host "PASS  $name" -ForegroundColor Green }
  else { Write-Host "FAIL  $name $detail" -ForegroundColor Red; $script:fail++ }
}
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$work = Join-Path ($env:RUNNER_TEMP ?? $env:TEMP) "kidspc-fresh"
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory $work | Out-Null
# The hub must be reached at an address other than 127.0.0.1, like a real PC reaching the TrueNAS.
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
$hub = "http://${ip}:$Port"
Check "a clean machine: no kid account yet" (-not (Get-LocalUser $KidUser -ErrorAction SilentlyContinue))
Check "a clean machine: no helper yet" (-not (Get-Service KidsPcHelper -ErrorAction SilentlyContinue))

# ------------------------------------------------------------------ hub, with this build on it
Push-Location "$root\hub"
$hash = python -m app.hashpw pw
Pop-Location
@"
timezone: America/New_York
parents: [{ name: Patrick, password_hash: "$hash" }]
kids:
  - { id: $KidUser, name: Barrett, device_token: dev-fresh-0123456789, extension_token: ext-fresh-0123456789 }
decisions: { web_mode: allowlist, game_pass_enabled: false, friend_verification: none, remote_access: lan_only }
lan_cidrs: ["$ip/32", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
managed_apps: [steam, chrome]
dns_filter: none
session_secret: fresh-pc-test-session-secret-0123456789
adguard: { enabled: false }
livekit: { url: "ws://127.0.0.1:7880", api_url: "http://127.0.0.1:9", api_key: k, api_secret: "fresh-pc-test-livekit-secret-0123456789", public_join_base: "$hub" }
"@ | Set-Content "$work\config.yaml" -Encoding UTF8
New-Item -ItemType Directory "$work\updates" | Out-Null
Compress-Archive "$BuildDir\*" "$work\updates\kidspc-win-x64.zip"
$zip = Get-Item "$work\updates\kidspc-win-x64.zip"
@{ version = ($env:GITHUB_SHA ?? "0000000000local"); sha256 = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower(); size = $zip.Length } |
  ConvertTo-Json | Set-Content "$work\updates\x64.json"

$env:HUB_CONFIG = "$work\config.yaml"; $env:HUB_DB = "$work\hub.db"; $env:HUB_DISABLE_POLLERS = "1"
New-NetFirewallRule -DisplayName "kidspc hub test" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
$hubProc = Start-Process python -ArgumentList "-m uvicorn app.main:app --host 0.0.0.0 --port $Port" -WorkingDirectory "$root\hub" `
  -PassThru -NoNewWindow -RedirectStandardError "$work\hub.err.log" -RedirectStandardOutput "$work\hub.out.log"
for ($i = 0; $i -lt 30; $i++) { try { Invoke-RestMethod "$hub/healthz" | Out-Null; break } catch { Start-Sleep 1 } }
Check "hub starts" (-not $hubProc.HasExited)

# ------------------------------------------------------------------ the parent's side: Set up a new PC
$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-RestMethod "$hub/api/parent/login" -Method Post -WebSession $s -ContentType application/json -Body '{"name":"Patrick","password":"pw"}' | Out-Null
$setup = Invoke-RestMethod "$hub/api/parent/kids/$KidUser/setup-code" -Method Post -WebSession $s
Write-Host "Dashboard line: $($setup.command)"
Check "dashboard gives one line to paste" ($setup.command -eq "irm $hub/setup/$($setup.code) | iex")
Check "the hub has the build for it" ($setup.builds.x64)

# ------------------------------------------------------------------ the PC's side: paste the line (twice)
$env:KIDSPC_KID_PASSWORD = "Fresh-Pc-Test-9!"   # stands in for the password the parent types at the prompt
$env:KIDSPC_NO_RESTART = "1"                   # a CI machine can't restart; a real PC does here
foreach ($run in 1, 2) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  powershell.exe -NoProfile -Command $setup.command *>&1 | Tee-Object "$work\setup-run$run.log" | Out-Host
  $out = Get-Content "$work\setup-run$run.log" -Raw
  Check "run $run of the pasted line finishes with 'PC is ready' ($([int]$sw.Elapsed.TotalSeconds) s)" ($out -match "PC is ready") ($out | Select-String "FAIL" | Out-String)
}

# ------------------------------------------------------------------ what Windows will use at the next start
$acct = Get-LocalUser $KidUser -ErrorAction SilentlyContinue
Check "kid account exists" ($null -ne $acct)
Check "kid account is not an administrator" (-not ((net localgroup (Get-LocalGroup -SID S-1-5-32-544).Name) -contains $KidUser))
$prof = Get-CimInstance Win32_UserProfile -Filter "SID='$($acct.SID.Value)'"
Check "kid profile made without signing in" ($prof -and (Test-Path "$($prof.LocalPath)\NTUSER.DAT"))
reg load "HKU\FreshKid" "$($prof.LocalPath)\NTUSER.DAT" | Out-Null
try {
  $shell = (Get-ItemProperty "Registry::HKEY_USERS\FreshKid\Software\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue).Shell
  Check "kid shell replaces Explorer for him" ($shell -eq "C:\Program Files\KidsPc\Shell\KidsPcShell.exe") $shell
  Check "Task Manager off for him" ((Get-ItemProperty "Registry::HKEY_USERS\FreshKid\Software\Microsoft\Windows\CurrentVersion\Policies\System").DisableTaskMgr -eq 1)
  Check "Windows key shortcuts and Quick Settings off for him" ((Get-ItemProperty "Registry::HKEY_USERS\FreshKid\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer").NoWinKeys -eq 1 -and (Get-ItemProperty "Registry::HKEY_USERS\FreshKid\Software\Policies\Microsoft\Windows\Explorer").DisableControlCenter -eq 1)
} finally { [gc]::Collect(); reg unload "HKU\FreshKid" | Out-Null }
Check "shell installed" (Test-Path "C:\Program Files\KidsPc\Shell\KidsPcShell.exe")
Check "helper service running" ((Get-Service KidsPcHelper).Status -eq "Running")
Check "helper starts with Windows" ((Get-Service KidsPcHelper).StartType -eq "Automatic")
$dev = Get-Content "$env:ProgramData\KidsPc\device.json" -Raw | ConvertFrom-Json
Check "device.json points at the hub" ($dev.hubUrl -eq $hub -and $dev.deviceToken -eq "dev-fresh-0123456789" -and $dev.kidUser -eq $KidUser)
$acl = (Get-Acl "$env:ProgramData\KidsPc\device.json").Access | ForEach-Object { $_.IdentityReference.Value }
Check "device token hidden from the kid" (-not ($acl -match "Users|$KidUser|Everyone")) ($acl -join ", ")

$wl = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Check "signs $KidUser in by himself" ($wl.AutoAdminLogon -eq "1" -and $wl.DefaultUserName -eq $KidUser) "$($wl.AutoAdminLogon) $($wl.DefaultUserName)"
Check "no plain-text password in the registry" ($null -eq $wl.DefaultPassword)
Check "no ForceAutoLogon (parents can still sign in)" ($null -eq $wl.ForceAutoLogon)
Add-Type -AssemblyName System.DirectoryServices.AccountManagement
$ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext([System.DirectoryServices.AccountManagement.ContextType]::Machine)
Check "the kid's password works" ($ctx.ValidateCredentials($KidUser, $env:KIDSPC_KID_PASSWORD))
Check "Chrome policies on" ((Get-ItemProperty "HKLM:\SOFTWARE\Policies\Google\Chrome").IncognitoModeAvailability -eq 1)
Check "Steam installed by the helper" (Test-Path "${env:ProgramFiles(x86)}\Steam\steam.exe")

# ------------------------------------------------------------------ the parent's dashboard sees the new PC
$kid = $null
for ($i = 0; $i -lt 12; $i++) {
  $kid = (Invoke-RestMethod "$hub/api/parent/overview" -WebSession $s).kids | Where-Object id -eq $KidUser
  if ($kid.online) { break }; Start-Sleep 5
}
Check "dashboard shows the PC online" ($kid.online)
$pc = $kid.apps | Where-Object app -eq "kidspc"
Check "dashboard shows the PC's build" ($pc -and $pc.detail -match "build") ($pc | ConvertTo-Json -Compress)
Check "dashboard shows Steam installed" (($kid.apps | Where-Object app -eq "steam").state -eq "installed") ($kid.apps | ConvertTo-Json -Compress)
$act = Invoke-RestMethod "$hub/api/parent/kids/$KidUser/activity" -WebSession $s
Check "dashboard activity says setup ran on this PC" ($act.events | Where-Object { $_.kind -eq "pc_setup" -and $_.detail -match $env:COMPUTERNAME })

# ------------------------------------------------------------------ a real sign-in as the kid, as Windows will do it
# Windows starts the per-user shell from his registry; here we start the same program as him to prove it
# comes up and reaches the helper. (A CI machine can't restart into automatic sign-in.)
# Not a hard check: a CI runner's desktop belongs to the runner's own account, so another account's window may not open there.
$cred = New-Object System.Management.Automation.PSCredential($KidUser, (ConvertTo-SecureString $env:KIDSPC_KID_PASSWORD -AsPlainText -Force))
try {
  $sh = Start-Process "C:\Program Files\KidsPc\Shell\KidsPcShell.exe" -Credential $cred -LoadUserProfile -PassThru -WorkingDirectory "C:\Program Files\KidsPc\Shell"
  Start-Sleep 15
  if ($sh.HasExited) { Write-Host "NOTE  kid shell started as $KidUser but exited ($($sh.ExitCode)) on the CI desktop" -ForegroundColor Yellow }
  else { Write-Host "PASS  kid shell starts as $KidUser and stays up" -ForegroundColor Green; $sh | Stop-Process -Force }
} catch { Write-Host "NOTE  could not start the shell as $KidUser on the CI desktop: $($_.Exception.Message)" -ForegroundColor Yellow }

Get-Content "$work\hub.err.log" -Tail 20 -ErrorAction SilentlyContinue
$hubProc | Stop-Process -Force -ErrorAction SilentlyContinue
if ($fail) { Write-Host "$fail check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host "New-PC setup: all checks passed" -ForegroundColor Green
