using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using KidsPc.Shared;

namespace KidsPc.Helper;

public sealed record AppStatus(string State, string Detail = "");

/// <summary>
/// Installs the launchers the hub says each PC should have (Steam, Chrome, Battle.net, Epic Games), silently and
/// as SYSTEM, so nobody has to sit at the PC. Reports progress to the hub in every heartbeat.
/// </summary>
public sealed class AppInstaller(DeviceConfig cfg, EventQueue events, ILogger<AppInstaller> log)
{
    // StaysOpen: the installer opens the app when it is done instead of exiting, so it is closed for it.
    private sealed record AppDef(string Name, Func<string> InstalledPath, Func<string> DownloadUrl, string FileName, Func<string, ProcessStartInfo> Installer,
        bool StaysOpen = false, string ByHand = "");

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
        // Battle.net's setup has no silent switch. With a language and a folder it asks nothing, but it
        // still opens the app's sign-in window at the end, which nobody can see from a service.
        ["battlenet"] = new("battlenet",
            () => cfg.BattleNetPath,
            () => "https://downloader.battle.net/download/getInstaller?os=win&installer=Battle.net-Setup.exe",
            "Battle.net-Setup.exe",
            f => new ProcessStartInfo(f, $"--lang=enUS --installpath=\"{Path.GetDirectoryName(cfg.BattleNetPath)}\""),
            StaysOpen: true,
            ByHand: "Battle.net would not install by itself. Sign in to an admin account on this PC and install it from battle.net."),
        // Epic ships a normal Windows installer package, which installs silently for the whole PC. The launcher runs
        // under x64 emulation on Windows on Arm. Its games then need a folder the kid's account may write to.
        ["epic"] = new("epic",
            () => Epic.LauncherExe,
            () => "https://launcher-public-service-prod06.ol.epicgames.com/launcher/api/installer/download/EpicGamesLauncherInstaller.msi",
            "EpicGamesLauncherInstaller.msi",
            f => new ProcessStartInfo("msiexec.exe", $"/i \"{f}\" /qn /norestart"),
            ByHand: "The Epic Games app would not install by itself. Sign in to an admin account on this PC and install it from store.epicgames.com."),
    };

    private bool _epicFolderReady;

    /// <summary>The Epic launcher runs as the kid, a standard account, and puts games in C:\Program Files\Epic Games,
    /// which only an admin may create or write to. So the helper makes that folder (and the launcher's records folder
    /// in ProgramData) and lets the kid's account write there, once per start. GUESS: without this the launcher asks
    /// for an admin password (a Windows prompt the kid can't answer) when it installs his first game.
    /// Anything he could put there himself is still closed on sight unless a parent approved it (Enforcer).</summary>
    private void PrepareEpicFolders()
    {
        if (_epicFolderReady) return;
        _epicFolderReady = true;   // once per helper start, whatever happens: a failure is logged, not retried every poll
        try
        {
            var dirs = new[] { cfg.EpicGamesRoot, Epic.DataDir }.Select(d => Directory.CreateDirectory(d)).ToList();
            if (string.IsNullOrEmpty(cfg.KidUser)) return;
            var sid = (SecurityIdentifier)new NTAccount(cfg.KidUser).Translate(typeof(SecurityIdentifier));
            foreach (var di in dirs)
            {
                var acl = di.GetAccessControl();
                acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.Modify,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
                di.SetAccessControl(acl);
            }
            log.LogInformation("Epic games folder ready for {Kid}", cfg.KidUser);
        }
        catch (Exception ex) { log.LogWarning("could not prepare the Epic games folder: {Msg}", ex.Message); }
    }

    private readonly ConcurrentDictionary<string, AppStatus> _status = new();
    private readonly ConcurrentDictionary<string, DateTimeOffset> _lastAttempt = new();
    private readonly HttpClient _download = new() { Timeout = TimeSpan.FromMinutes(20) };

    public IReadOnlyDictionary<string, AppStatus> Status => _status;

    /// <summary>Called every poll. Starts at most one background install per app, retrying failures every 30 minutes.</summary>
    public void Ensure(IEnumerable<string> apps)
    {
        foreach (var name in apps)
        {
            if (!_defs.TryGetValue(name, out var def)) continue;
            // An install in flight is not done when the app's program appears: the installer is still writing,
            // and may have a window to close. InstallAsync sets the result when it is.
            if (_status.TryGetValue(name, out var cur) && cur.State == "installing") continue;
            if (File.Exists(def.InstalledPath()))
            {
                _status[name] = new("installed");
                if (name == "epic") PrepareEpicFolders();   // also for a launcher a parent installed by hand
                continue;
            }
            if (_lastAttempt.TryGetValue(name, out var last) && DateTimeOffset.Now - last < TimeSpan.FromMinutes(30)) continue;

            _lastAttempt[name] = DateTimeOffset.Now;
            _status[name] = new("installing");
            _ = Task.Run(() => InstallAsync(def));
        }
    }

    /// <summary>The kid asked for a game whose launcher is missing: get it installed soon, without letting
    /// him restart a failing install every time he taps. Returns where the launcher stands now.</summary>
    public AppStatus Want(string name)
    {
        if (!_defs.TryGetValue(name, out var def)) return new("failed", "unknown app");
        if (_status.TryGetValue(name, out var cur) && cur.State == "installing") return cur;
        if (File.Exists(def.InstalledPath())) return _status[name] = new("installed");
        var failedBefore = cur?.State == "failed";
        if (!_lastAttempt.TryGetValue(name, out var last) || DateTimeOffset.Now - last > TimeSpan.FromMinutes(5))
        {
            _lastAttempt.TryRemove(name, out _);
            Ensure([name]);
        }
        // "failed" stays the answer while a retry starts, so he is told a parent is needed rather than "a few minutes" forever.
        if (failedBefore) return new("failed", cur!.Detail);
        return _status.TryGetValue(name, out var now) ? now : new("installing");
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
            if (def.StaysOpen) await WaitThenCloseAsync(def, p);
            else await p.WaitForExitAsync().WaitAsync(TimeSpan.FromMinutes(20));

            // Steam's /S installer exits before it finishes copying; give it a moment.
            for (var i = 0; i < 30 && !File.Exists(def.InstalledPath()); i++) await Task.Delay(2000);
            _status[def.Name] = File.Exists(def.InstalledPath())
                ? new("installed")
                : new("failed", def.ByHand != "" ? def.ByHand
                    : $"installer exited {(p.HasExited ? p.ExitCode : -1)} but {Path.GetFileName(def.InstalledPath())} is missing");
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

    /// <summary>For an installer that does not exit by itself: wait for the app to appear, let it finish
    /// writing, then close the installer and the app it opened. Only this service's own session is touched,
    /// so a launcher the kid is using is never closed.</summary>
    private async Task WaitThenCloseAsync(AppDef def, Process setup)
    {
        var giveUp = DateTimeOffset.Now + TimeSpan.FromMinutes(15);
        DateTimeOffset? exited = null;
        while (DateTimeOffset.Now < giveUp && !File.Exists(def.InstalledPath()))
        {
            // The first program may hand over to a second one and exit; allow that one a few minutes too.
            if (setup.HasExited) exited ??= DateTimeOffset.Now;
            if (exited is { } at && DateTimeOffset.Now - at > TimeSpan.FromMinutes(5)) break;
            await Task.Delay(2000);
        }
        // The app's own program appears before the last files do: wait until its folder has been quiet for a while.
        var folder = Path.GetDirectoryName(def.InstalledPath())!;
        for (var i = 0; i < 60 && File.Exists(def.InstalledPath()) && !Quiet(folder, TimeSpan.FromSeconds(20)); i++) await Task.Delay(5000);

        try { if (!setup.HasExited) setup.Kill(entireProcessTree: true); } catch { /* already gone */ }
        var mine = Process.GetCurrentProcess().SessionId;
        var appName = Path.GetFileNameWithoutExtension(def.InstalledPath());
        var appMarker = Path.DirectorySeparatorChar + Path.GetFileName(folder) + Path.DirectorySeparatorChar;   // "\Battle.net\"
        foreach (var proc in Process.GetProcesses())
        {
            try
            {
                if (proc.SessionId != mine || proc.Id == Environment.ProcessId) continue;
                // The app itself, the installer, and the app's background updater (found by its folder, its name is too plain).
                var ours = proc.ProcessName.Equals(appName, StringComparison.OrdinalIgnoreCase)
                        || proc.ProcessName.Equals(Path.GetFileNameWithoutExtension(def.FileName), StringComparison.OrdinalIgnoreCase)
                        || (proc.ProcessName.Equals("Agent", StringComparison.OrdinalIgnoreCase)
                            && (proc.MainModule?.FileName ?? "").Contains(appMarker, StringComparison.OrdinalIgnoreCase));
                if (ours) proc.Kill(entireProcessTree: true);
            }
            catch { /* it ended by itself, or it is not ours to look at */ }
            finally { proc.Dispose(); }
        }
    }

    /// <summary>Nothing in the folder was written for this long.</summary>
    private static bool Quiet(string folder, TimeSpan howLong)
    {
        try
        {
            var newest = new DirectoryInfo(folder).EnumerateFiles("*", new EnumerationOptions { RecurseSubdirectories = true, IgnoreInaccessible = true })
                .Select(f => f.LastWriteTimeUtc).DefaultIfEmpty(DateTime.MinValue).Max();
            return DateTime.UtcNow - newest > howLong;
        }
        catch { return true; }      // can't look: don't wait on it
    }
}
