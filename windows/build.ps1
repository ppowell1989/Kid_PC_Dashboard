# Builds the shell and helper into .\publish\<rid> (self-contained). Needs the .NET 8 SDK.
#   .\build.ps1                 # x64 (the real kid PCs)
#   .\build.ps1 -Runtime win-arm64   # Windows on ARM (e.g. a UTM/Parallels VM on an Apple Silicon Mac)
param([ValidateSet("win-x64", "win-arm64")] [string] $Runtime = "win-x64")
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
dotnet publish src\KidsPc.Shell\KidsPc.Shell.csproj -c Release -r $Runtime --self-contained -o publish\$Runtime\Shell
dotnet publish src\KidsPc.Helper\KidsPc.Helper.csproj -c Release -r $Runtime --self-contained -o publish\$Runtime\Helper
Pop-Location
Write-Host "Built to $PSScriptRoot\publish\$Runtime. Next: setup\Install-KidPc.ps1 -BuildDir $PSScriptRoot\publish\$Runtime"
