# Kids PC setup for __KIDNAME__, served by the hub at __HUB__.
# Run in PowerShell opened as administrator:   irm __HUB__/setup/__CODE__ | iex
# It asks the hub for this PC's tokens, downloads the newest kid shell and helper from the hub,
# and runs the installer that ships with them. Safe to run again if anything goes wrong.
& {
  $ErrorActionPreference = "Stop"
  $ProgressPreference = "SilentlyContinue"   # Windows PowerShell's progress bar makes downloads 10x slower
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $hub = "__HUB__"; $code = "__CODE__"

  Write-Host ""
  Write-Host "  Kids PC setup: making this __KIDNAME__'s PC" -ForegroundColor Cyan
  Write-Host ""
  $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Open PowerShell as administrator (right-click Start, Terminal (Admin)) and paste the line again." -ForegroundColor Red
    return
  }

  # 12 = ARM64 (the Mac test VM), 9 = x64 (the real PCs). The CPU, not this PowerShell, which may be emulated.
  $arch = if ((Get-CimInstance Win32_Processor | Select-Object -First 1).Architecture -eq 12) { "arm64" } else { "x64" }
  Write-Host "==> Asking the hub for this PC's settings ($arch)" -ForegroundColor Cyan
  try {
    $claim = Invoke-RestMethod "$hub/api/setup/$code/claim" -Method Post -ContentType "application/json" `
      -Body (@{ computer = $env:COMPUTERNAME; arch = $arch } | ConvertTo-Json)
  } catch {
    $msg = $_.ErrorDetails.Message; if (-not $msg) { $msg = $_.Exception.Message }
    Write-Host "The hub said no: $msg" -ForegroundColor Red
    Write-Host "Make a new setup code on the dashboard (the kid's card, Set up a new PC) and try again." -ForegroundColor Red
    return
  }

  $work = Join-Path $env:ProgramData "KidsPc\setup"
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $work | Out-Null
  $zip = Join-Path $work "kidspc.zip"
  Write-Host "==> Downloading build $($claim.build.version.Substring(0, 8)) from the hub" -ForegroundColor Cyan
  Invoke-WebRequest "$hub/api/device/update/file?arch=$arch" -Headers @{ Authorization = "Bearer $($claim.device_token)" } -OutFile $zip -UseBasicParsing
  $sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
  if ($sha -ne $claim.build.sha256) { throw "The download is damaged (checksum mismatch). Run the line again." }
  Expand-Archive $zip -DestinationPath "$work\bundle" -Force

  # A PC that was set up before keeps its Windows account name (the Mac test VM uses adamkid).
  $user = $claim.windows_user
  $prev = Join-Path $env:ProgramData "KidsPc\device.json"
  if (Test-Path $prev) { try { $old = (Get-Content $prev -Raw | ConvertFrom-Json).kidUser; if ($old) { $user = $old } } catch { } }
  if ($env:KIDSPC_USER) { $user = $env:KIDSPC_USER }

  Set-ExecutionPolicy -Scope Process Bypass -Force
  & "$work\bundle\setup\Install-KidPc.ps1" -KidUser $user -KidName $claim.kid_name -HubUrl $hub `
      -DeviceToken $claim.device_token -ExtensionToken $claim.extension_token -WebMode $claim.web_mode `
      -BuildDir "$work\bundle" -AutoLogon -Restart:($env:KIDSPC_NO_RESTART -ne "1")
}
