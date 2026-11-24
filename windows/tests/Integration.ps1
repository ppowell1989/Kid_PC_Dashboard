<#
  End-to-end check on a Windows machine (used by CI, also runnable by hand from the repo root as admin):
  starts the hub, seeds it, runs the helper as a console app, talks to it over the named pipe
  exactly like the shell does, and starts the shell to make sure it stays up.
  Usage: .\windows\tests\Integration.ps1 -BuildDir windows\publish\win-x64
  -LauncherInstall also waits for the helper to install Battle.net by itself (minutes, and it depends on
  Blizzard's servers), so CI runs that in its own job where it cannot hold up a build.
  -AsSystem starts the helper as the SYSTEM account with no desktop, the way the installed service runs,
  instead of as the signed-in admin. That is the honest test of an installer that wants to show a window.
#>
param([Parameter(Mandatory)] [string] $BuildDir, [int] $Port = 8765, [switch] $LauncherInstall, [switch] $AsSystem)
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
  - { id: adam, name: Adam, device_token: dev-adam, extension_token: ext-adam, daily_minutes: { weekday: 90, weekend: 90 }, bedtime: { weekday: "", weekend: "" } }   # no bedtime: the checks must pass at any hour CI runs
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
# A Battle.net game on a PC without Battle.net: approving it is what makes the helper fetch the launcher.
$wow = Invoke-RestMethod "$hub/api/parent/games" -Method Post -WebSession $s -ContentType application/json `
  -Body '{"title":"World of Warcraft","launcher":"battlenet","launch_ref":"WoW","approved_for":["adam"]}'
$bnExe = "${env:ProgramFiles(x86)}\Battle.net\Battle.net.exe"

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

if ($AsSystem) {
  $helperExe = (Resolve-Path "$BuildDir\Helper\KidsPcHelper.exe").Path
  $run = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$helperExe`" > `"$work\helper.out.log`" 2> `"$work\helper.err.log`"`""
  Register-ScheduledTask -TaskName "KidsPcHelperTest" -Action $run -User "SYSTEM" -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask "KidsPcHelperTest"
  $helper = $null
  for ($i = 0; $i -lt 20 -and -not $helper; $i++) { Start-Sleep 1; $helper = Get-Process KidsPcHelper -ErrorAction SilentlyContinue | Select-Object -First 1 }
  if (-not $helper) { throw "the helper did not start as SYSTEM" }
  Write-Host "INFO  helper runs as SYSTEM in session $($helper.SessionId)"
} else {
  $helper = Start-Process "$BuildDir\Helper\KidsPcHelper.exe" -PassThru -NoNewWindow `
    -RedirectStandardOutput "$work\helper.out.log" -RedirectStandardError "$work\helper.err.log"
}
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
Check "pipe: only Adam's approved games listed" ((@($st.data.rules.games.title) -join ",") -eq "Portal 2,World of Warcraft") ($st.data.rules.games | ConvertTo-Json)
Check "pipe: an approved Battle.net game puts Battle.net on the list of apps to install" (@($st.data.rules.managed_apps) -contains "battlenet") ($st.data.rules.managed_apps -join ",")
Check "pipe: not locked with time left" ($st.data.locked -eq $false)
Check "pipe: hub reachable" ($st.data.hubReachable -eq $true)

# A game whose launcher is not on the PC yet: the kid is asked to come back, in plain words, and the shell
# is never pointed at a program that is not there (that is what put Windows' error text on his screen).
# (steam.exe appears before Steam's installer is done, so "is the file there" does not say which answer is right.)
$ins = Pipe @{ op = "install"; gameId = $g.id }
if ($ins.data.action -ne "wait") {
  Check "install approved steam game -> steam://install" ($ins.data.uri -eq "steam://install/620") ($ins | ConvertTo-Json)
} else {
  Check "install while Steam is being set up -> wait, in plain words" (-not $ins.data.uri -and $ins.data.message -match "Steam is being set up") ($ins | ConvertTo-Json)
}
$bn = Pipe @{ op = "install"; gameId = $wow.id }
$bnWaited = $bn.data.action -eq "wait"
if ($bnWaited) {
  Check "install a Battle.net game without Battle.net -> wait, in plain words" ($bn.data.action -eq "wait" -and -not $bn.data.uri -and $bn.data.message -match "Battle.net (is being set up|needs a parent)") ($bn | ConvertTo-Json)
}
$oops = Pipe @{ op = "problem"; detail = "Chrome could not start: The system cannot find the file specified." }
Check "the shell can report something that would not start" $oops.ok ($oops | ConvertTo-Json)
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
# The helper polls for commands between other work (Chrome history upload, app installs), so wait up to 30 s
# for the message rather than a fixed 8 s, which raced with that work on slow runners.
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep 1
  $st = Pipe @{ op = "state" }
  $act = Invoke-RestMethod "$hub/api/parent/kids/adam/activity" -WebSession $s
  if (@($st.data.messages | Where-Object { $_.text -eq "Dinner in 5!" }).Count -eq 1 -and ($act.commands | Where-Object { $_.kind -eq "message" }).state -eq "done") { break }
}
Check "parent message reaches the shell" (@($st.data.messages | Where-Object { $_.text -eq "Dinner in 5!" }).Count -eq 1) ($st.data.messages | ConvertTo-Json)
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
# Approving Portal 2 put it on the PC's install list: with Steam there, the helper tells Steam about it by itself
# (a manifest marked "needs downloading") and reports where it stands. Nobody is signed in here, so it waits for the kid.
$acf = "${env:ProgramFiles(x86)}\Steam\steamapps\appmanifest_620.acf"
$pcRow = $null
for ($i = 0; $i -lt 20; $i++) {
  $inst = Invoke-RestMethod "$hub/api/parent/installs" -WebSession $s
  $pcRow = @($inst.pcs | Where-Object { $_.id -eq "adam" })[0]
  $p2 = @($pcRow.games | Where-Object { $_.title -eq "Portal 2" })[0]
  if ((Test-Path $acf) -and $p2.state -notin "queued") { break }
  Start-Sleep 3
}
Check "approved game: helper gave Steam its manifest by itself" ((Test-Path $acf) -and ((Get-Content $acf -Raw) -match '"StateFlags"\s+"1026"')) (Get-Content $acf -Raw -ErrorAction SilentlyContinue)
Check "unapproved game: no manifest" (-not (Test-Path "${env:ProgramFiles(x86)}\Steam\steamapps\appmanifest_999999.acf"))
Check "approved game: hub shows where it stands, in plain words" ($p2.state -in "waiting", "needs_signin" -and $p2.plain.text) ($p2 | ConvertTo-Json -Depth 4)
Write-Host "INFO  Portal 2 on the PCs page: [$($p2.plain.label)] $($p2.plain.text)"
$wowRow = @($pcRow.games | Where-Object { $_.title -eq "World of Warcraft" })[0]
Write-Host "INFO  World of Warcraft on the PCs page: state=$($wowRow.state) [$($wowRow.plain.label)] $($wowRow.plain.text)"
Check "battle.net game waits for Battle.net instead of failing" ($wowRow.state -notin "failed", "manual") ($wowRow | ConvertTo-Json -Depth 4)
Check "hub knows the PC's free space" ($pcRow.disk_free_gb -gt 0) ($pcRow | ConvertTo-Json -Depth 2)
$st = Pipe @{ op = "state" }
Check "a game Steam hasn't downloaded yet is not on the home screen" (@($st.data.installedGameIds) -notcontains $g.id) ($st.data.installedGameIds -join ",")
Check "its catalogue tile says it is on its way" ($st.data.installing."$($g.id)" -match "On its way|Downloading|Installing") ($st.data.installing | ConvertTo-Json)

$ins = Pipe @{ op = "install"; gameId = $g.id }
Check "install approved steam game once Steam is there -> steam://install" ($ins.data.uri -eq "steam://install/620") ($ins | ConvertTo-Json)
if ($bnWaited) {
  Check "parents are told a launcher is missing" (($act.events | Where-Object { $_.kind -eq "launcher_missing" -and $_.detail -match "World of Warcraft" }) -ne $null) ($act.events | ConvertTo-Json)
}
Check "parents are told when something would not start" (($act.events | Where-Object { $_.kind -eq "launch_failed" -and $_.detail -match "Chrome could not start" }) -ne $null) ($act.events | ConvertTo-Json)

if ($LauncherInstall) {
  # Battle.net has no silent installer. The helper runs it unattended, waits for the app to appear and
  # closes the sign-in window it leaves open. This is the only place that is tried on a real Windows.
  $bnApp = $null; $began = Get-Date; $exeAfter = $null
  for ($i = 0; $i -lt 240; $i++) {   # up to 20 minutes; the helper gives up by itself after 15
    # The installer restarts network pieces of Windows; a dropped connection to the hub here is not a result.
    try { $ov = Invoke-RestMethod "$hub/api/parent/overview" -WebSession $s } catch { Write-Host "INFO  hub did not answer once: $_"; Start-Sleep 5; continue }
    $bnApp = @($ov.kids[0].apps) | Where-Object { $_.app -eq "battlenet" }
    if (-not $exeAfter -and (Test-Path $bnExe)) { $exeAfter = [int]((Get-Date) - $began).TotalSeconds }
    if ($bnApp.state -in "installed", "failed") { break }
    Start-Sleep 5
  }
  Write-Host "INFO  Battle.net: state=$($bnApp.state) detail=$($bnApp.detail) exe=$(Test-Path $bnExe) program appeared after ${exeAfter}s, finished after $([int]((Get-Date) - $began).TotalSeconds)s"
  Get-ChildItem (Split-Path $bnExe) -ErrorAction SilentlyContinue | Select-Object -First 12 | ForEach-Object { Write-Host "INFO    $($_.Name)" }
  Get-Process "Battle.net*", "Agent" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "INFO    still running: $($_.Name) in session $($_.SessionId)" }
  Check "helper installed Battle.net by itself and reported it" ($bnApp.state -eq "installed" -and (Test-Path $bnExe)) ($bnApp | ConvertTo-Json)
  # "installed" is only said once the installer and the sign-in window it opens have been closed.
  Check "the install left no Battle.net window or installer running" (@(Get-Process "Battle.net", "Battle.net-Setup" -ErrorAction SilentlyContinue).Count -eq 0) ((Get-Process "Battle.net*" -ErrorAction SilentlyContinue | ForEach-Object Name) -join ",")
  $bn = Pipe @{ op = "install"; gameId = $wow.id }
  Check "install a Battle.net game once Battle.net is there -> starts Battle.net" ($bn.data.action -eq "exec" -and $bn.data.uri -eq $bnExe) ($bn | ConvertTo-Json)
}

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
if ($AsSystem) { Unregister-ScheduledTask -TaskName "KidsPcHelperTest" -Confirm:$false -ErrorAction SilentlyContinue }
Stop-Process $hubProc -Force -ErrorAction SilentlyContinue
Write-Host "`n--- helper log ---"; Get-Content "$work\helper.out.log" -ErrorAction SilentlyContinue | Select-Object -Last 30
if ($fail) { Write-Host "$fail check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host "All checks passed" -ForegroundColor Green
