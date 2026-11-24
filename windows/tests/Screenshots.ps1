<#
  Takes a real screenshot of every screen of the kid shell, on a real Windows desktop.
  The shell runs in demo mode (canned data, no hub or helper needed, nothing launches).
  Used by CI; also runnable by hand:
    .\windows\tests\Screenshots.ps1 -BuildDir windows\publish\win-x64 -Out shots
#>
param([Parameter(Mandatory)] [string] $BuildDir, [string] $Out = "shots", [int] $Width = 1920, [int] $Height = 1080)
$ErrorActionPreference = "Stop"
# Every screen in the default look, then a few screens in each of the other themes. "theme/scene"; no theme = clean.
$shots = "home", "catalogue", "ask", "chat", "voice", "warning", "lock", "themes",
         "blocks/home", "blocks/ask", "blocks/voice", "blocks/themes",
         "royale/home", "royale/chat", "royale/lock",
         "speedway/home", "speedway/catalogue", "speedway/warning",
         "retro/home", "retro/voice", "retro/lock"
New-Item -ItemType Directory -Force $Out | Out-Null
$exe = (Resolve-Path "$BuildDir\Shell\KidsPcShell.exe").Path
$env:KIDSPC_DEMO_OUT = (Resolve-Path $Out).Path

# CI runners start at 1024x768. The kids' monitors are 1080p, so look at the design at that size.
try { Set-DisplayResolution -Width $Width -Height $Height -Force | Out-Null } catch { Write-Host "Could not change the resolution: $_" }
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
Write-Host "Screen is $($bounds.Width)x$($bounds.Height)"

[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point(($bounds.Width - 2), ($bounds.Height - 2))   # off the tiles, so nothing is caught mid-hover

$fail = 0
foreach ($shot in $shots) {
  $theme, $scene = if ($shot -match "/") { $shot -split "/" } else { "clean", $shot }
  $name = if ($theme -eq "clean") { $scene } else { "$theme-$scene" }
  Remove-Item "$Out\voice-page.txt" -ErrorAction SilentlyContinue
  $p = Start-Process $exe -ArgumentList "--demo", $scene, "--theme", $theme -PassThru
  Start-Sleep 9     # window up, game art downloaded
  if ($scene -eq "voice") {
    # The voice room is a WebView2 page. Its first start on a fresh machine is slow, so wait until the shell
    # reports that the page has loaded (it writes voice-page.txt in demo runs).
    for ($i = 0; $i -lt 40 -and -not (Test-Path "$Out\voice-page.txt"); $i++) { Start-Sleep 1 }
    if (Test-Path "$Out\voice-page.txt") { Write-Host "voice page: $(Get-Content "$Out\voice-page.txt" -Raw)" } else { Write-Host "voice page: nothing reported after 49 s" }
    Start-Sleep 2
  }
  if ($p.HasExited) { Write-Host "FAIL  ${name}: the shell exited with code $($p.ExitCode)" -ForegroundColor Red; $fail++; continue }
  $bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
  $bmp.Save((Join-Path $Out "$name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  Write-Host "PASS  $name" -ForegroundColor Green
  Stop-Process $p -Force
  Start-Sleep 1
}
Remove-Item "$Out\voice-page.txt", "$Out\voice-page.png" -ErrorAction SilentlyContinue
if ($fail) { Write-Host "$fail scene(s) failed" -ForegroundColor Red; exit 1 }
