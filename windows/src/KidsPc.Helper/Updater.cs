using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>
/// Pulls new builds of the helper and shell from the hub and installs them, so the PCs update
/// themselves. The swap is done by a one-shot SYSTEM scheduled task (a service can't overwrite its
/// own files), and only while the shell isn't running: when the kid is signed out, or right after a
/// restart, which a parent can trigger from the dashboard.
/// </summary>
public sealed class Updater(HttpClient http, ILogger<Updater> log) : BackgroundService
{
    private sealed record BuildInfo(string Version, string Sha256, long Size);

    private static readonly string Arch = RuntimeInformation.OSArchitecture == Architecture.Arm64 ? "arm64" : "x64";
    private static readonly string InstallDir = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
    private static readonly string StagedRoot = Path.Combine(DeviceConfig.Dir, "staged");
    private string? _lastApplied;
    private DateTimeOffset _lastApplyAt;

    public static string HelperVersion =>
        Assembly.GetEntryAssembly()?.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "dev";

    public static string ShellVersion
    {
        get
        {
            var exe = Path.Combine(InstallDir, "Shell", "KidsPcShell.exe");
            return File.Exists(exe) ? FileVersionInfo.GetVersionInfo(exe).ProductVersion ?? "dev" : "missing";
        }
    }

    public AppStatus Status { get; private set; } = new("installed", $"version {Short(HelperVersion)}");

    private static string Short(string v) => v.Length > 8 ? v[..8] : v;

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await Task.Delay(TimeSpan.FromSeconds(45), ct);
        while (!ct.IsCancellationRequested)
        {
            try { await CheckAsync(ct); }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                log.LogWarning("update check failed: {Msg}", ex.Message);
                Status = new("failed", ex.Message);
            }
            await Task.Delay(TimeSpan.FromMinutes(2), ct);
        }
    }

    private async Task CheckAsync(CancellationToken ct)
    {
        var r = await http.GetAsync($"/api/device/update?arch={Arch}", ct);
        if (r.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            Status = new("installed", $"version {Short(HelperVersion)} (hub has no build to offer)");
            return;
        }
        r.EnsureSuccessStatusCode();
        var build = (await r.Content.ReadFromJsonAsync<BuildInfo>(Json.Options, ct))!;
        if (build.Version == HelperVersion && build.Version == ShellVersion)
        {
            Status = new("installed", $"up to date ({Short(build.Version)})");
            return;
        }

        var staged = Path.Combine(StagedRoot, build.Version);
        if (!File.Exists(Path.Combine(staged, ".ok")))
        {
            Status = new("installing", $"downloading {Short(build.Version)}");
            await DownloadAsync(build, staged, ct);
        }

        if (Process.GetProcessesByName("KidsPcShell").Length > 0)
        {
            Status = new("installing", $"update {Short(build.Version)} ready; applies at sign-out or restart");
            return;
        }
        if (_lastApplied == build.Version && DateTimeOffset.Now - _lastApplyAt < TimeSpan.FromMinutes(30))
        {
            Status = new("failed", $"update {Short(build.Version)} did not take; see C:\\ProgramData\\KidsPc\\update.log");
            return;
        }
        _lastApplied = build.Version;
        _lastApplyAt = DateTimeOffset.Now;
        Status = new("installing", $"installing {Short(build.Version)}");
        Apply(staged);
    }

    private async Task DownloadAsync(BuildInfo build, string staged, CancellationToken ct)
    {
        Directory.CreateDirectory(StagedRoot);
        foreach (var old in Directory.GetDirectories(StagedRoot).Where(d => d != staged))
            try { Directory.Delete(old, true); } catch { /* in use; next time */ }

        var zip = staged + ".zip";
        await using (var src = await http.GetStreamAsync($"/api/device/update/file?arch={Arch}", ct))
        await using (var dst = File.Create(zip))
            await src.CopyToAsync(dst, ct);

        await using (var f = File.OpenRead(zip))
        {
            var hash = Convert.ToHexString(await SHA256.HashDataAsync(f, ct));
            if (!hash.Equals(build.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("downloaded build failed its checksum");
        }
        if (Directory.Exists(staged)) Directory.Delete(staged, true);
        ZipFile.ExtractToDirectory(zip, staged);
        File.Delete(zip);
        if (!File.Exists(Path.Combine(staged, "Helper", "KidsPcHelper.exe")))
            throw new InvalidDataException("build is missing Helper\\KidsPcHelper.exe");
        await File.WriteAllTextAsync(Path.Combine(staged, ".ok"), build.Version, ct);
    }

    private const string Script = """
        param([string]$Staged, [string]$InstallDir)
        $log = "$env:ProgramData\KidsPc\update.log"
        function Log($m) { "$(Get-Date -Format o)  $m" | Add-Content $log }
        Log "update from $Staged"
        try {
          Stop-Service KidsPcHelper -Force
          (Get-Service KidsPcHelper).WaitForStatus('Stopped', '00:00:30')
          Start-Sleep 2
          Copy-Item -Recurse -Force "$Staged\Helper\*" "$InstallDir\Helper\"
          Log "helper copied"
          if (Get-Process KidsPcShell -ErrorAction SilentlyContinue) { Log "shell is running; shell update deferred" }
          else { Copy-Item -Recurse -Force "$Staged\Shell\*" "$InstallDir\Shell\"; Log "shell copied" }
        } catch { Log ($_ | Out-String) }
        Start-Service KidsPcHelper
        Log "done"
        """;

    private void Apply(string staged)
    {
        var script = Path.Combine(DeviceConfig.Dir, "update.ps1");
        File.WriteAllText(script, Script);
        var cmd = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script}\" -Staged \"{staged}\" -InstallDir \"{InstallDir}\"";
        Run("schtasks.exe", $"/Create /F /RU SYSTEM /RL HIGHEST /SC ONCE /ST 00:00 /TN KidsPcUpdate /TR \"{cmd.Replace("\"", "\\\"")}\"");
        Run("schtasks.exe", "/Run /TN KidsPcUpdate");
        log.LogInformation("update task started for {Staged}", staged);
    }

    private static void Run(string exe, string args)
    {
        using var p = Process.Start(new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true })!;
        p.WaitForExit(30_000);
        if (p.ExitCode != 0) throw new InvalidOperationException($"{exe} {args.Split(' ')[0]} failed: {p.StandardError.ReadToEnd()}");
    }
}
