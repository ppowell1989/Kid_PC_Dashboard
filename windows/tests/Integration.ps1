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
managed_apps: [steam, chrome]
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
@{ hubUrl = $hub; deviceToken = "dev-adam"; kidUser = "nobody-at-console"; pollSeconds = 3; historySeconds = 5 } | ConvertTo-Json | Set-Content "$dataDir\device.json" -Encoding UTF8
Remove-Item "$dataDir\history-since.txt" -ErrorAction SilentlyContinue

# A Chrome History file for the (fake) kid, with Chrome's real table layout, so the history reader has something to send.
$hist = "C:\Users\nobody-at-console\AppData\Local\Google\Chrome\User Data\Default"
New-Item -ItemType Directory -Force $hist | Out-Null
@'
import sqlite3, sys, time
now = int((time.time() + 11644473600) * 1_000_000)   # Chrome time: microseconds since 1601
db = sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE urls(id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, visit_count INTEGER DEFAULT 0, typed_count INTEGER DEFAULT 0, last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0);
CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER NOT NULL, visit_time INTEGER NOT NULL, from_visit INTEGER, transition INTEGER DEFAULT 0);
CREATE TABLE keyword_search_terms(keyword_id INTEGER NOT NULL, url_id INTEGER NOT NULL, term LONGVARCHAR NOT NULL, normalized_term LONGVARCHAR NOT NULL);
""")
db.execute("INSERT INTO urls VALUES(1,'https://www.google.com/search?q=sexy+pics&udm=14','sexy pics - Google Search',1,0,?,0)", (now,))
db.execute("INSERT INTO urls VALUES(2,'https://en.wikipedia.org/wiki/Cat','Cat - Wikipedia',1,0,?,0)", (now,))
db.execute("INSERT INTO visits VALUES(1,1,?,0,0)", (now,))
db.execute("INSERT INTO visits VALUES(2,2,?,0,0)", (now,))
db.execute("INSERT INTO keyword_search_terms VALUES(2,1,'sexy pics','sexy pics')")
db.commit()
'@ | Set-Content "$work\mkhist.py"
python "$work\mkhist.py" "$hist\History"
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

# Remote control from the dashboard: a message reaches the shell, pause locks, resume unlocks.
$cmd = Invoke-RestMethod "$hub/api/parent/kids/adam/commands" -Method Post -WebSession $s -ContentType application/json -Body '{"kind":"message","arg":"Dinner in 5!"}'
Start-Sleep 8
$st = Pipe @{ op = "state" }
Check "parent message reaches the shell" (@($st.data.messages | Where-Object { $_.text -eq "Dinner in 5!" }).Count -eq 1) ($st.data.messages | ConvertTo-Json)
$act = Invoke-RestMethod "$hub/api/parent/kids/adam/activity" -WebSession $s
Check "command marked done on the hub" (($act.commands | Where-Object { $_.kind -eq "message" }).state -eq "done")
Invoke-RestMethod "$hub/api/parent/kids/adam/pause" -Method Post -WebSession $s -ContentType application/json -Body '{"minutes":0}' | Out-Null
Start-Sleep 8
Check "pause now locks the PC" ((Pipe @{ op = "state" }).data.locked -eq $true)
Invoke-RestMethod "$hub/api/parent/kids/adam/pause" -Method Delete -WebSession $s | Out-Null
Start-Sleep 8
Check "resume unlocks the PC" ((Pipe @{ op = "state" }).data.locked -eq $false)

# Web filter: family DNS on the adapters, Chrome secure DNS pinned to it.
$dnsSet = @(Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object { $_.ServerAddresses }) -contains "1.1.1.3"
Check "PC uses the family DNS filter" $dnsSet (Get-DnsClientServerAddress | Out-String)
$cp = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Google\Chrome"
Check "chrome secure DNS pinned to the family filter" ($cp.DnsOverHttpsMode -eq "secure" -and $cp.DnsOverHttpsTemplates -like "https://family.*")
Check "kid game search answers" ((Pipe @{ op = "game_search"; detail = "portal" }).ok)

# Chrome history reader: pages and searches reach the dashboard, flagged words light up.
$srch = @(); for ($i = 0; $i -lt 10 -and $srch.Count -eq 0; $i++) { Start-Sleep 3; $srch = @(Invoke-RestMethod "$hub/api/parent/searches/adam" -WebSession $s) }
Check "chrome history: search reached the hub and is flagged" (@($srch | Where-Object { $_.term -eq "sexy pics" -and $_.flagged -eq 1 }).Count -eq 1) (Get-Content "$work\helper.out.log" -Tail 20 | Out-String)
$pages = Invoke-RestMethod "$hub/api/parent/visits/adam" -WebSession $s
Check "chrome history: page visit reached the hub" (@($pages | Where-Object { $_.domain -eq "en.wikipedia.org" }).Count -ge 1)
$cp2 = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Google\Chrome"
Check "chrome search goes to Google Web results (no AI Overview)" ($cp2.DefaultSearchProviderSearchURL -like "*udm=14*")

# Family chat both ways through the pipe.
Invoke-RestMethod "$hub/api/parent/chat" -Method Post -WebSession $s -ContentType application/json -Body '{"text":"Homework first"}' | Out-Null
$chat = Pipe @{ op = "chat"; detail = "0" }
Check "kid sees the family chat" (@($chat.data | Where-Object { $_.text -eq "Homework first" -and -not $_.mine }).Count -eq 1) ($chat | ConvertTo-Json -Depth 5)
$sent = Pipe @{ op = "chat_send"; detail = "ok fine" }
$pc = Invoke-RestMethod "$hub/api/parent/chat" -WebSession $s
Check "kid's chat reaches the parents" ($sent.ok -and @($pc | Where-Object { $_.text -eq "ok fine" -and $_.sender -eq "Adam" }).Count -eq 1) ($pc | ConvertTo-Json)

# Managed apps: the helper should install Steam on its own (Chrome is preinstalled on CI runners).
$steamOk = $false
for ($i = 0; $i -lt 60 -and -not $steamOk; $i++) {
  $ov = Invoke-RestMethod "$hub/api/parent/overview" -WebSession $s
  $apps = @($ov.kids[0].apps)
  $steamOk = ($apps | Where-Object { $_.app -eq "steam" -and $_.state -eq "installed" }) -ne $null
  if (($apps | Where-Object { $_.app -eq "steam" -and $_.state -eq "failed" })) { break }
  if (-not $steamOk) { Start-Sleep 5 }
}
Check "helper installed Steam silently and reported it" $steamOk ($apps | ConvertTo-Json)
Check "steam.exe exists" (Test-Path "${env:ProgramFiles(x86)}\Steam\steam.exe")
Check "hub shows chrome installed" (($apps | Where-Object { $_.app -eq "chrome" -and $_.state -eq "installed" }) -ne $null)
$kp = $apps | Where-Object { $_.app -eq "kidspc" }
Check "helper reports its build" ($kp -ne $null -and $kp.state -eq "installed" -and $kp.detail -match "on build") ($kp | ConvertTo-Json)
$act = Invoke-RestMethod "$hub/api/parent/kids/adam/activity" -WebSession $s
Check "steam install reported in the activity feed" (($act.events | Where-Object { $_.kind -eq "app_installed" -and $_.detail -eq "steam" }) -ne $null)

# Lock: a parent sets every day to 0 minutes in Playtimes (the hub's DB owns the rules, not config.yaml).
Invoke-RestMethod "$hub/api/parent/playtimes/adam" -Method Put -WebSession $s -ContentType application/json `
  -Body '{"days":[0,1,2,3,4,5,6],"daily_minutes":0,"bedtime":"23:59","windows":[]}' | Out-Null
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
