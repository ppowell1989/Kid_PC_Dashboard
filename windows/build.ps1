# Builds the shell and helper into ..\windows\publish (self-contained, win-x64). Needs the .NET 8 SDK.
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
dotnet publish src\KidsPc.Shell\KidsPc.Shell.csproj -c Release -o publish\Shell
dotnet publish src\KidsPc.Helper\KidsPc.Helper.csproj -c Release -o publish\Helper
Pop-Location
Write-Host "Built to $PSScriptRoot\publish. Next: setup\Install-KidPc.ps1 -BuildDir $PSScriptRoot\publish"
