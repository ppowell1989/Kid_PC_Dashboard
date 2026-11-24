using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using KidsPc.Shared;
using Microsoft.Win32;

namespace KidsPc.Helper;

/// <summary>
/// Pulls new builds of the helper and shell from the hub and installs them, so the PCs update
/// themselves. The swap is done by a one-shot SYSTEM scheduled task (a service can't overwrite its
/// own files) and needs the shell closed. So a downloaded build goes in when nobody is signed in,
/// or in the first minutes after the kid's screen starts: he is told, signed out, the build is
/// installed, and the PC restarts if it signs him in by itself. That is what makes "restart the PC"
/// install a waiting update even though Windows signs the kid in seconds after starting, before
/// this service gets a look. A game in progress is never interrupted, with one exception: a parent
/// pressing "Install now" on the dashboard. A parent can also set a PC (a test PC, say) to take
/// every build the moment it arrives, as long as no game is running.
/// </summary>
public sealed class Updater(HttpClient http, DeviceConfig cfg, StateCache cache, GameLibrary library, ILogger<Updater> log) : BackgroundService
{
    private sealed record BuildInfo(string Version, string Sha256, long Size);

    private static readonly string Arch = RuntimeInformation.OSArchitecture == Architecture.Arm64 ? "arm64" : "x64";
    private static readonly string InstallDir = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, ".."));
    private static readonly string StagedRoot = Path.Combine(DeviceConfig.Dir, "staged");
    private static readonly string ForcedFile = Path.Combine(DeviceConfig.Dir, "update-forced.txt");

    private static readonly TimeSpan Tick = TimeSpan.FromSeconds(20);
    private static readonly TimeSpan HubCheckEvery = TimeSpan.FromSeconds(30);
    /// <summary>How long after his screen starts the kid may be signed out again for an update.</summary>
    private static readonly TimeSpan FreshSignIn = TimeSpan.FromMinutes(3);
    private static readonly TimeSpan ForceAgainAfter = TimeSpan.FromMinutes(30);
    private const int MaxForcedPerBuild = 2;
    /// <summary>Ticks in a row with nobody signed in before installing, so an automatic sign-in that is
    /// still on its way (at start-up) cannot start the shell in the middle of the copy.</summary>
    private const int QuietTicksNeeded = 4;

    private string? _ready;            // version of a build that is downloaded and waiting to go in
    private int _quietTicks;
    private DateTimeOffset _lastTick;
    private string? _lastApplied;
    private DateTimeOffset _lastApplyAt;
    private string? _askedBy;                               // a parent pressed "Install now" on the dashboard
    private readonly SemaphoreSlim _wake = new(0, 1);       // cuts the wait between ticks short

    /// <summary>A parent asked for the newest build to go in now, whatever the kid is doing.</summary>
    public string InstallNow(string by)
    {
        Interlocked.Exchange(ref _askedBy, string.IsNullOrWhiteSpace(by) ? "A parent" : by);
        try { _wake.Release(); } catch (SemaphoreFullException) { /* already awake */ }
        return _ready is { } v ? $"installing {Short(v)}" : "checking for a newer build";
    }

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

    public AppStatus Status { get; private set; } = new("installed", $"on build {Short(HelperVersion)}");

    private static string Short(string v) => v.Length > 8 ? v[..8] : v;

    private static bool Installed(string version) => version == HelperVersion && version == ShellVersion;

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // A build downloaded before the PC restarted is installed without asking the hub again:
        // the network is often not up yet in the minutes when it can go in.
        _ready = StagedOnDisk();
        await _wake.WaitAsync(Tick, ct);
        var nextHubCheck = DateTimeOffset.MinValue;
        while (!ct.IsCancellationRequested)
        {
            // One try per press: a build that will not go in must not keep signing him out.
            var askedBy = Interlocked.Exchange(ref _askedBy, null);
            if (DateTimeOffset.Now >= nextHubCheck || askedBy is not null)
            {
                nextHubCheck = DateTimeOffset.Now + HubCheckEvery;
                try { await CheckAsync(ct); }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    log.LogWarning("update check failed: {Msg}", ex.Message);
                    if (_ready is null) Status = new("failed", ex.Message);
                }
            }
            if (_ready is { } version)
            {
                try { await InstallWhenPossibleAsync(version, askedBy, ct); }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    log.LogWarning("update install failed: {Msg}", ex.Message);
                    Status = new("failed", ex.Message);
                }
            }
            await _wake.WaitAsync(Tick, ct);
        }
    }

    /// <summary>Asks the hub for its build and downloads it if this PC is on a different one.</summary>
    private async Task CheckAsync(CancellationToken ct)
    {
        var r = await http.GetAsync($"/api/device/update?arch={Arch}", ct);
        if (r.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            if (_ready is null) Status = new("installed", $"on build {Short(HelperVersion)} (the hub has no build to offer)");
            return;
        }
        r.EnsureSuccessStatusCode();
        var build = (await r.Content.ReadFromJsonAsync<BuildInfo>(Json.Options, ct))!;
        if (Installed(build.Version))
        {
            _ready = null;
            Status = new("installed", $"on build {Short(build.Version)}");
            return;
        }

        var staged = Path.Combine(StagedRoot, build.Version);
        if (!File.Exists(Path.Combine(staged, ".ok")))
        {
            Status = new("installing", $"downloading {Short(build.Version)}");
            await DownloadAsync(build, staged, ct);
        }
        _ready = build.Version;
    }

    /// <summary>A complete downloaded build that is not the one installed, if there is one.</summary>
    private static string? StagedOnDisk()
    {
        try
        {
            if (!Directory.Exists(StagedRoot)) return null;
            foreach (var dir in Directory.GetDirectories(StagedRoot))
            {
                var ok = Path.Combine(dir, ".ok");
                if (!File.Exists(ok)) continue;
                var version = File.ReadAllText(ok).Trim();
                if (version == Path.GetFileName(dir) && !Installed(version)) return version;
            }
        }
        catch { /* unreadable: the hub check sorts it out */ }
        return null;
    }

    private async Task InstallWhenPossibleAsync(string version, string? askedBy, CancellationToken ct)
    {
        var staged = Path.Combine(StagedRoot, version);
        var waiting = new AppStatus("installing", $"update {Short(version)} ready; installs at the next restart or sign-in");
        var rightAway = cache.Rules?.AutoUpdate == true;    // the parent chose "install updates as soon as they arrive" for this PC

        // A long gap between ticks means the PC was asleep or shut down with fast start-up: treat it like a fresh start.
        var now = DateTimeOffset.UtcNow;
        if (now - _lastTick > Tick * 3) _quietTicks = 0;
        _lastTick = now;

        var shells = Process.GetProcessesByName("KidsPcShell");
        if (shells.Length == 0)
        {
            // Nobody at the shell. Before touching its files, be sure the kid is not in the middle of being signed in.
            var user = Sessions.ConsoleUser();
            var someoneElse = !string.IsNullOrEmpty(user) && !string.Equals(user, cfg.KidUser, StringComparison.OrdinalIgnoreCase);
            _quietTicks++;
            if (!someoneElse && askedBy is null && _quietTicks < QuietTicksNeeded) { Status = waiting; return; }
            if (askedBy is null && _lastApplied == version && DateTimeOffset.Now - _lastApplyAt < ForceAgainAfter)
            {
                Status = new("failed", $"update {Short(version)} did not take; see C:\\ProgramData\\KidsPc\\update.log");
                return;
            }
            _lastApplied = version;
            _lastApplyAt = DateTimeOffset.Now;
            Status = new("installing", $"installing {Short(version)}");
            Apply(staged, restart: false);
            return;
        }

        // The kid's screen is up, and its files can't be replaced while it runs. Just after it started
        // (a restart or a sign-in) nothing is lost by signing him out for a minute, so the build goes in then.
        // A PC set to install right away does the same whenever no game is running, and a parent's
        // "Install now" does it whatever is running.
        _quietTicks = 0;
        if (askedBy is null)
        {
            if (!(JustStarted(shells) || rightAway) || !MayForce(version) || GameRunning())
            {
                Status = rightAway && MayForce(version)
                    ? new("installing", $"update {Short(version)} ready; installs when the game is closed")
                    : waiting;
                return;
            }
        }

        var restart = SignsInByItself();
        RecordForced(version);      // before signing anyone out: this is what stops it from ever looping
        Status = new("installing", $"installing {Short(version)}");
        log.LogInformation("signing the kid out to install {Version} (restart after: {Restart}, asked by: {By})", version, restart, askedBy ?? "nobody");
        var then = restart ? "This PC will restart in a moment." : "You will be signed out for a moment.";
        cache.AddMessage(new ParentMessage(-(int)(DateTimeOffset.Now.ToUnixTimeSeconds() & 0x3FFFFFFF), askedBy ?? "Kids PC",
            askedBy is null ? $"Installing an update. {then}" : $"{askedBy} is installing an update. {then}", DateTimeOffset.Now));
        await Task.Delay(TimeSpan.FromSeconds(8), ct);      // long enough for the shell to show it
        try
        {
            foreach (var session in shells.Select(p => (uint)p.SessionId).Distinct()) Sessions.SignOut(session);
            for (var i = 0; i < 60 && Process.GetProcessesByName("KidsPcShell").Length > 0; i++) await Task.Delay(1000, ct);
            if (Process.GetProcessesByName("KidsPcShell").Length > 0)
            {
                log.LogWarning("the kid's session did not sign out; update {Version} left for later", version);
                Status = waiting;
                return;
            }
            _lastApplied = version;
            _lastApplyAt = DateTimeOffset.Now;
            Apply(staged, restart);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            // He may already be signed out. On a PC that signs him in by itself, never leave him at a password prompt.
            if (restart && Process.GetProcessesByName("KidsPcShell").Length == 0)
                Process.Start(new ProcessStartInfo("shutdown.exe", "/r /t 5 /c \"Kids PC is restarting.\"") { UseShellExecute = false, CreateNoWindow = true });
            throw;
        }
    }

    private static bool JustStarted(Process[] shells)
    {
        try { return shells.All(p => DateTime.Now - p.StartTime < FreshSignIn); }
        catch { return false; }     // can't tell: leave him alone
    }

    private bool GameRunning()
    {
        try { return library.CurrentActivity(cache.Rules, Sessions.ConsoleSessionId).Processes.Any(p => p.Kind is not ("chrome" or "browser")); }
        catch (Exception ex)
        {
            log.LogWarning("could not tell whether a game is running: {Msg}", ex.Message);
            return true;            // can't tell: leave him alone
        }
    }

    /// <summary>Windows signs an account in by itself at start-up (Sysinternals Autologon), so after a restart the kid is back without typing a password.</summary>
    private static bool SignsInByItself()
    {
        using var k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon");
        return k?.GetValue("AutoAdminLogon")?.ToString() == "1";
    }

    // The record of sign-outs done for an update, kept on disk because each install restarts this service.
    // A build that will not install must not sign the kid out again and again: twice per build, half an hour apart, at most.
    private static (string Version, int Count, DateTimeOffset At)? LastForced()
    {
        try
        {
            var parts = File.ReadAllText(ForcedFile).Trim().Split('|');
            return (parts[0], int.Parse(parts[1], CultureInfo.InvariantCulture), DateTimeOffset.Parse(parts[2], CultureInfo.InvariantCulture));
        }
        catch { return null; }
    }

    private static bool MayForce(string version)
    {
        if (LastForced() is not { } last || last.Version != version) return true;
        return last.Count < MaxForcedPerBuild && DateTimeOffset.Now - last.At > ForceAgainAfter;
    }

    private static void RecordForced(string version)
    {
        var count = LastForced() is { } last && last.Version == version ? last.Count + 1 : 1;
        File.WriteAllText(ForcedFile, $"{version}|{count}|{DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture)}");
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
        if (!File.Exists(Path.Combine(staged, "Helper", "KidsPcHelper.exe")) || !File.Exists(Path.Combine(staged, "Shell", "KidsPcShell.exe")))
            throw new InvalidDataException("build is incomplete (missing the helper or shell)");
        await File.WriteAllTextAsync(Path.Combine(staged, ".ok"), build.Version, ct);
    }

    private const string Script = """
        param([string]$Staged, [string]$InstallDir, [switch]$Restart)
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
        if ($Restart) { Log "restarting so the kid is signed back in"; shutdown.exe /r /t 5 /c "Kids PC update installed." }
        Log "done"
        """;

    private void Apply(string staged, bool restart)
    {
        var script = Path.Combine(DeviceConfig.Dir, "update.ps1");
        File.WriteAllText(script, Script);
        var cmd = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{script}\" -Staged \"{staged}\" -InstallDir \"{InstallDir}\"{(restart ? " -Restart" : "")}";
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
