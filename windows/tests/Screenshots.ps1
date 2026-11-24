<#
  Takes a real screenshot of every screen of the kid shell, on a real Windows desktop.
  The shell runs in demo mode (canned data, no hub or helper needed, nothing launches).
  Used by CI; also runnable by hand:
    .\windows\tests\Screenshots.ps1 -BuildDir windows\publish\win-x64 -Out shots
#>
param([Parameter(Mandatory)] [string] $BuildDir, [string] $Out = "shots", [int] $Width = 1920, [int] $Height = 1080)
$ErrorActionPreference = "Stop"
$scenes = "home", "catalogue", "ask", "chat", "warning", "lock"
New-Item -ItemType Directory -Force $Out | Out-Null
$exe = (Resolve-Path "$BuildDir\Shell\KidsPcShell.exe").Path

# CI runners start at 1024x768. The kids' monitors are 1080p, so look at the design at that size.
try { Set-DisplayResolution -Width $Width -Height $Height -Force | Out-Null } catch { Write-Host "Could not change the resolution: $_" }
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
Write-Host "Screen is $($bounds.Width)x$($bounds.Height)"

[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point(($bounds.Width - 2), ($bounds.Height - 2))   # off the tiles, so nothing is caught mid-hover

$fail = 0
foreach ($scene in $scenes) {
  $p = Start-Process $exe -ArgumentList "--demo", $scene -PassThru
  Start-Sleep 9     # window up, game art downloaded
  if ($p.HasExited) { Write-Host "FAIL  ${scene}: the shell exited with code $($p.ExitCode)" -ForegroundColor Red; $fail++; continue }
  $bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
  $bmp.Save((Join-Path $Out "$scene.png"), [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  Write-Host "PASS  $scene" -ForegroundColor Green
  Stop-Process $p -Force
  Start-Sleep 1
}
if ($fail) { Write-Host "$fail scene(s) failed" -ForegroundColor Red; exit 1 }
