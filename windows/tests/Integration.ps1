<#
  End-to-end check on a Windows machine (used by CI, also runnable by hand from the repo root as admin):
  starts the hub, seeds it, runs the helper as a console app, talks to it over the named pipe
  exactly like the shell does, and starts the shell to make sure it stays up.
  Usage: .\windows\tests\Integration.ps1 -BuildDir windows\publish\win-x64
#>
param([Parameter(Mandatory)] [string] $BuildDir, [int] $Port = 8765)
$ErrorActionPreference = "Stop"
$fail = 0
trap {
  Write-Host "ERROR: $_" -ForegroundColor Red
  foreach ($f in "helper.out.log", "helper.err.log", "hub.err.log") {
    Write-Host "--- $f ---"; Get-Content (Join-Path $work $f) -ErrorAction SilentlyContinue | Select-Object -Last 40
  }
  exit 1
}
function Check($name, $cond, $detail = "") {
  if ($cond) { Write-Host "PASS  $name" -ForegroundColor Green }
  else { Write-Host "FAIL  $name $detail" -ForegroundColor Red; $script:fail++ }
}
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$work = Join-Path ($env:RUNNER_TEMP ?? $env:TEMP) "kidspc-it"
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory $work | Out-Null
$hub = "http://127.0.0.1:$Port"

# ------------------------------------------------------------------ hub
Push-Location "$root\hub"
$hash = python -m app.hashpw pw
Pop-Location
@"
timezone: America/New_York
parents: [{ name: Patrick, password_hash: "$hash" }]
kids:
  - { id: adam, name: Adam, device_token: dev-adam, extension_token: ext-adam, daily_minutes: { weekday: 90, weekend: 90 }, bedtime: { weekday: "23:59", weekend: "23:59" } }
decisions: { web_mode: allowlist, game_pass_enabled: false, friend_verification: none, remote_access: lan_only }
allowed_sites: [wikipedia.org]
session_secret: integration-test-session-secret-0123456789
adguard: { enabled: false }
livekit: { url: "ws://127.0.0.1:7880", api_url: "http://127.0.0.1:9", api_key: k, api_secret: "integration-test-livekit-secret-0123456789", public_join_base: "http://127.0.0.1:$Port" }
"@ | Set-Content "$work\config.yaml" -Encoding UTF8

$env:HUB_CONFIG = "$work\config.yaml"; $env:HUB_DB = "$work\hub.db"
$hubProc = Start-Process python -ArgumentList "-m uvicorn app.main:app --port $Port" -WorkingDirectory "$root\hub" `
  -PassThru -NoNewWindow -RedirectStandardError "$work\hub.err.log" -RedirectStandardOutput "$work\hub.out.log"
for ($i = 0; $i -lt 30; $i++) { try { Invoke-RestMethod "$hub/healthz" | Out-Null; break } catch { Start-Sleep 1 } }
Check "hub starts" ($hubProc.HasExited -eq $false)

$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-RestMethod "$hub/api/parent/login" -Method Post -WebSession $s -ContentType application/json -Body '{"name":"Patrick","password":"pw"}' | Out-Null
$g = Invoke-RestMethod "$hub/api/parent/games" -Method Post -WebSession $s -ContentType application/json `
  -Body '{"title":"Portal 2","launcher":"steam","launch_ref":"620","approved_for":["adam"]}'
Invoke-RestMethod "$hub/api/parent/games" -Method Post -WebSession $s -ContentType application/json `
  -Body '{"title":"Not For Adam","launcher":"steam","launch_ref":"999999","approved_for":[]}' | Out-Null

# ------------------------------------------------------------------ helper (console mode)
$dataDir = "$env:ProgramData\KidsPc"
New-Item -ItemType Directory -Force $dataDir | Out-Null
@{ hubUrl = $hub; deviceToken = "dev-adam"; kidUser = "nobody-at-console"; pollSeconds = 3 } | ConvertTo-Json | Set-Content "$dataDir\device.json" -Encoding UTF8
Remove-Item -Recurse -Force "HKLM:\SOFTWARE\Policies\Google\Chrome\URLBlocklist" -ErrorAction SilentlyContinue

$helper = Start-Process "$BuildDir\Helper\KidsPcHelper.exe" -PassThru -NoNewWindow `
  -RedirectStandardOutput "$work\helper.out.log" -RedirectStandardError "$work\helper.err.log"
Start-Sleep 8
Check "helper stays up" ($helper.HasExited -eq $false) (Get-Content "$work\helper.err.log" -Raw -ErrorAction SilentlyContinue)

function Pipe($obj) {
  $c = New-Object System.IO.Pipes.NamedPipeClientStream(".", "KidsPcHelper", [System.IO.Pipes.PipeDirection]::InOut)
  $c.Connect(5000)
  $w = New-Object System.IO.StreamWriter($c); $w.AutoFlush = $true
  $r = New-Object System.IO.StreamReader($c)
  $w.WriteLine(($obj | ConvertTo-Json -Compress))
  $line = $r.ReadLine(); $c.Dispose()
  if (-not $line) { throw "helper closed the pipe without replying to $($obj.op)" }
  return $line | ConvertFrom-Json
}

$st = Pipe @{ op = "state" }
Check "pipe: state ok" $st.ok ($st | ConvertTo-Json -Depth 6)
Check "pipe: rules came from hub (kid name)" ($st.data.rules.kid.name -eq "Adam")
Check "pipe: only Adam's approved game listed" (($st.data.rules.games | Measure-Object).Count -eq 1 -and $st.data.rules.games[0].title -eq "Portal 2")
Check "pipe: not locked with time left" ($st.data.locked -eq $false)
Check "pipe: hub reachable" ($st.data.hubReachable -eq $true)

$ins = Pipe @{ op = "install"; gameId = $g.id }
Check "install approved steam game -> steam://install" ($ins.data.uri -eq "steam://install/620") ($ins | ConvertTo-Json)
$bad = Pipe @{ op = "install"; gameId = 9999 }
Check "install unapproved game refused" ($bad.data.action -eq "refused")

$req = Pipe @{ op = "request"; kind = "time"; detail = "15" }
Check "pipe: time request sent" $req.ok ($req | ConvertTo-Json)
$pending = Invoke-RestMethod "$hub/api/parent/requests" -WebSession $s
Check "hub received the request" (($pending | Where-Object { $_.kind -eq "time" -and $_.detail -eq "15" }) -ne $null)

$ov = Invoke-RestMethod "$hub/api/parent/overview" -WebSession $s
Check "hub saw a heartbeat" ($ov.kids[0].online -eq $true)

$bl = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Google\Chrome\URLBlocklist" -ErrorAction SilentlyContinue
$al = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Google\Chrome\URLAllowlist" -ErrorAction SilentlyContinue
Check "chrome allow-list mode: block *" ($bl.'1' -eq "*")
Check "chrome allow-list includes wikipedia.org" (@($al.PSObject.Properties.Value) -contains "wikipedia.org")

# Lock: use up the day via a big negative grant is not possible, so approve nothing and set usage by heartbeats is slow;
# instead flip the kid to 0 minutes by editing config and restarting the hub.
Stop-Process $hubProc -Force
(Get-Content "$work\config.yaml") -replace "weekday: 90, weekend: 90", "weekday: 0, weekend: 0" | Set-Content "$work\config.yaml"
$hubProc = Start-Process python -ArgumentList "-m uvicorn app.main:app --port $Port" -WorkingDirectory "$root\hub" -PassThru -NoNewWindow `
  -RedirectStandardError "$work\hub2.err.log" -RedirectStandardOutput "$work\hub2.out.log"
Start-Sleep 10
$st = Pipe @{ op = "state" }
Check "locked when out of time" ($st.data.locked -eq $true) ($st.data.rules.time | ConvertTo-Json)
$vt = Pipe @{ op = "voice_token" }
Check "voice refused while locked" ($vt.ok -eq $false)

# ------------------------------------------------------------------ shell
$shell = Start-Process "$BuildDir\Shell\KidsPcShell.exe" -PassThru
Start-Sleep 10
Check "shell stays up for 10s" ($shell.HasExited -eq $false)
try {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  [System.Drawing.Graphics]::FromImage($bmp).CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
  $bmp.Save("$work\shell.png")
} catch { Write-Host "screenshot skipped: $_" }

Get-Process KidsPcShell -ErrorAction SilentlyContinue | Stop-Process -Force
Stop-Process $helper -Force -ErrorAction SilentlyContinue
Stop-Process $hubProc -Force -ErrorAction SilentlyContinue
Write-Host "`n--- helper log ---"; Get-Content "$work\helper.out.log" -ErrorAction SilentlyContinue | Select-Object -Last 30
if ($fail) { Write-Host "$fail check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host "All checks passed" -ForegroundColor Green
