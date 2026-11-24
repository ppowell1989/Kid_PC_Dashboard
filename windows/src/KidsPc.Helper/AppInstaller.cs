using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.InteropServices;
using KidsPc.Shared;

namespace KidsPc.Helper;

public sealed record AppStatus(string State, string Detail = "");

/// <summary>
/// Installs the launchers the hub says each PC should have (Steam, Chrome), silently and as SYSTEM,
/// so nobody has to sit at the PC. Reports progress to the hub in every heartbeat.
/// </summary>
public sealed class AppInstaller(DeviceConfig cfg, EventQueue events, ILogger<AppInstaller> log)
{
    private sealed record AppDef(string Name, Func<string> InstalledPath, Func<string> DownloadUrl, string FileName, Func<string, ProcessStartInfo> Installer);

    private static readonly bool Arm64 = RuntimeInformation.OSArchitecture == Architecture.Arm64;
    private static readonly string Pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
    private static readonly string Pf86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);

    private readonly Dictionary<string, AppDef> _defs = new()
    {
        ["steam"] = new("steam",
            () => Path.Combine(Pf86, "Steam", "steam.exe"),
            () => "https://cdn.cloudflare.steamstatic.com/client/installer/SteamSetup.exe",
            "SteamSetup.exe",
            f => new ProcessStartInfo(f, "/S")),
        ["chrome"] = new("chrome",
            () => Path.Combine(Pf, "Google", "Chrome", "Application", "chrome.exe"),
            () => Arm64
                ? "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise_arm64.msi"
                : "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi",
            "chrome.msi",
            f => new ProcessStartInfo("msiexec.exe", $"/i \"{f}\" /qn /norestart")),
    };

    private readonly ConcurrentDictionary<string, AppStatus> _status = new();
    private readonly ConcurrentDictionary<string, DateTimeOffset> _lastAttempt = new();
    private readonly HttpClient _download = new() { Timeout = TimeSpan.FromMinutes(20) };

    public IReadOnlyDictionary<string, AppStatus> Status => _status;

    /// <summary>Called every poll. Starts at most one background install per app, retrying failures every 30 minutes.</summary>
    public void Ensure(IEnumerable<string> apps)
    {
        foreach (var name in apps)
        {
            if (name == "battlenet")
            {
                // Battle.net's installer always shows its own window; it can't be installed unattended.
                _status[name] = File.Exists(cfg.BattleNetPath) ? new("installed") : new("manual", "Install Battle.net from an admin account");
                continue;
            }
            if (!_defs.TryGetValue(name, out var def)) continue;
            if (File.Exists(def.InstalledPath())) { _status[name] = new("installed"); continue; }
            if (_status.TryGetValue(name, out var cur) && cur.State == "installing") continue;
            if (_lastAttempt.TryGetValue(name, out var last) && DateTimeOffset.Now - last < TimeSpan.FromMinutes(30)) continue;

            _lastAttempt[name] = DateTimeOffset.Now;
            _status[name] = new("installing");
            _ = Task.Run(() => InstallAsync(def));
        }
    }

    /// <summary>Parent pressed "install" on the dashboard: try now, ignoring the retry back-off.</summary>
    public void InstallNow(string name)
    {
        _lastAttempt.TryRemove(name, out _);
        if (_status.TryGetValue(name, out var cur) && cur.State == "failed") _status.TryRemove(name, out _);
        Ensure([name]);
    }

    private async Task InstallAsync(AppDef def)
    {
        var dir = Path.Combine(DeviceConfig.Dir, "downloads");
        Directory.CreateDirectory(dir);
        var file = Path.Combine(dir, def.FileName);
        try
        {
            log.LogInformation("installing {App}", def.Name);
            await using (var src = await _download.GetStreamAsync(def.DownloadUrl()))
            await using (var dst = File.Create(file))
                await src.CopyToAsync(dst);

            var psi = def.Installer(file);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            using var p = Process.Start(psi)!;
            await p.WaitForExitAsync().WaitAsync(TimeSpan.FromMinutes(20));

            // Steam's /S installer exits before it finishes copying; give it a moment.
            for (var i = 0; i < 30 && !File.Exists(def.InstalledPath()); i++) await Task.Delay(2000);
            _status[def.Name] = File.Exists(def.InstalledPath())
                ? new("installed")
                : new("failed", $"installer exited {p.ExitCode} but {Path.GetFileName(def.InstalledPath())} is missing");
            events.Add(_status[def.Name].State == "installed" ? "app_installed" : "app_install_failed", def.Name);
        }
        catch (Exception ex)
        {
            log.LogWarning("install {App} failed: {Msg}", def.Name, ex.Message);
            _status[def.Name] = new("failed", ex.Message);
            events.Add("app_install_failed", $"{def.Name}: {ex.Message}");
        }
        finally
        {
            try { File.Delete(file); } catch { /* best effort */ }
        }
    }
}
