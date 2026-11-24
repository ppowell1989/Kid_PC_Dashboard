using System.Diagnostics;
using System.Security.Principal;
using System.Text.RegularExpressions;
using KidsPc.Shared;
using Microsoft.Win32;

namespace KidsPc.Helper;

/// <summary>Where one approved game stands on this PC, as reported to the hub. Retry is the hub's "Try again" round it belongs to.
/// Stage is a few words on exactly what the store is doing right now ("Waiting for Steam", "Downloading", "Installing",
/// "Checking files", "Paused"): it goes on the kid's tile and the parents' PCs page, so "on its way" is never all anyone sees.</summary>
public sealed record GameInstallStatus(string State, string Detail = "", long DoneBytes = 0, long TotalBytes = 0, int Retry = 0, string Stage = "");

/// <summary>
/// Puts the games on the hub's install list onto this PC by themselves, like software pushed out by an IT office:
/// a parent approves a game, and nobody has to touch the PC. Runs every poll from <see cref="HubSync"/> and reports
/// each game's state, with progress, in the next heartbeat.
///
/// Steam: the helper writes the game's appmanifest into Steam's library marked "needs downloading", and gets Steam
/// (which runs as the kid, signed in to his account) to read it, by starting or restarting Steam in his session through
/// the shell. Steam then downloads it like any update, and its manifest says how far it got.
/// Battle.net: has no supported way to be told from outside. The shell runs Battle.net's --exec="install CODE" (GUESS)
/// and the helper watches the game's folder grow.
/// Installer share ("direct" games): the helper runs the installer itself, silently, as SYSTEM.
/// What a store needs from a person (signing in once, owning the game) is found and reported in plain words.
/// </summary>
public sealed partial class GameDeployer(DeviceConfig cfg, GameLibrary library, AppInstaller apps, EventQueue events, ILogger<GameDeployer> log)
{
    private sealed class Attempt
    {
        public int Retry;
        public DateTimeOffset Started = DateTimeOffset.Now;
        public bool ManifestWritten;
        public bool NeedsKick;
        public DateTimeOffset? KickedAt;
        public bool Restarted;                // the kick restarted Steam (rather than starting it)
        public long LogOffset = -1;           // Steam's content_log.txt length when this attempt began: only newer lines are about it
        public long LastSize = -1;            // Battle.net: folder size last time, and since when it hasn't changed
        public DateTimeOffset SizeSince = DateTimeOffset.Now;
        public DateTimeOffset SizedAt = DateTimeOffset.MinValue;
        public Task? Running;                 // installer-share install in flight
    }

    private readonly object _gate = new();
    private readonly Dictionary<int, GameInstallStatus> _status = [];
    private readonly Dictionary<int, Attempt> _attempts = [];
    private readonly List<(Nudge Nudge, DateTimeOffset At)> _nudges = [];
    private DateTimeOffset _lastSteamKick = DateTimeOffset.MinValue;
    private DateTimeOffset? _steamSeenSince;     // when Steam was first seen running in the kid's session this time
    private string _kidName = "him";
    private Dictionary<int, string> _launcherOf = [];

    // Steam's StateFlags bits (from the Steam client's EAppState).
    private const long UpdateRequired = 2, FullyInstalled = 4, UpdateRunning = 256, UpdatePaused = 512, UpdateStarted = 1024,
        Validating = 131072, Preallocating = 524288, Downloading = 1048576, Staging = 2097152, Committing = 4194304;
    private const long Busy = UpdateRunning | Validating | Preallocating | Downloading | Staging | Committing;

    private static readonly TimeSpan KickGap = TimeSpan.FromMinutes(3);       // at most one Steam restart this often
    private static readonly TimeSpan StuckAfter = TimeSpan.FromMinutes(10);   // a store that was nudged and did nothing for this long

    public IReadOnlyDictionary<int, GameInstallStatus> Status { get { lock (_gate) return new Dictionary<int, GameInstallStatus>(_status); } }

    /// <summary>Things for the shell to do in the kid's session. Each stays on offer for two minutes; the shell does each Id once.</summary>
    public List<Nudge> Nudges()
    {
        lock (_gate)
        {
            _nudges.RemoveAll(n => DateTimeOffset.Now - n.At > TimeSpan.FromMinutes(2));
            return _nudges.Select(n => n.Nudge).ToList();
        }
    }

    /// <summary>Games on their way, with the few words their tile shows instead of "Install" or "Play": what is really
    /// happening ("Waiting for Steam", "Downloading 45%"), or that a grown-up has to do something first.</summary>
    public Dictionary<int, string> Captions()
    {
        lock (_gate)
            return _status.Where(kv => kv.Value.State is "installing" or "waiting" or "queued" or "needs_signin" or "not_owned" or "no_space" or "stuck" or "failed")
                .ToDictionary(kv => kv.Key, kv => KidWords(kv.Value));
    }

    private static string KidWords(GameInstallStatus s) => s.State switch
    {
        "installing" when Pct(s) is { } p && s.Stage is "" or "Downloading" => $"Downloading {p}%",
        "installing" => s.Stage is "" ? "Installing" : s.Stage,
        "waiting" or "queued" => s.Stage is "" ? "Starting soon" : s.Stage,
        _ => "Waiting for Mom or Dad",
    };

    private static int? Pct(GameInstallStatus s) => s.TotalBytes > 0 ? (int)Math.Min(99, s.DoneBytes * 100 / s.TotalBytes) : null;

    /// <summary>How far each download is, for the bar on its tile.</summary>
    public Dictionary<int, int> Percents()
    {
        lock (_gate)
            return _status.Where(kv => kv.Value.State == "installing" && Pct(kv.Value) is not null)
                .ToDictionary(kv => kv.Key, kv => Pct(kv.Value)!.Value);
    }

    /// <summary>Stores waiting for someone to sign in on this PC. The shell leaves their windows on screen for that.</summary>
    public List<string> SignInNeeded()
    {
        lock (_gate)
            return _status.Where(kv => kv.Value.State == "needs_signin" && _launcherOf.ContainsKey(kv.Key))
                .Select(kv => _launcherOf[kv.Key]).Distinct().ToList();
    }

    /// <summary>Free space on the drive games go to, in GB, for the parents' PC page.</summary>
    public double? FreeGb()
    {
        try { return Math.Round(new DriveInfo(Path.GetPathRoot(cfg.SteamPath)!).AvailableFreeSpace / (double)(1L << 30), 1); }
        catch { return null; }
    }

    public void Tick(Rules rules, bool kidHere, bool gameRunning)
    {
        _kidName = rules.Kid.Name;
        _launcherOf = rules.Games.ToDictionary(g => g.Id, g => g.Launcher);
        ReportBattleNetProducts();
        var wanted = (rules.Installs ?? []).GroupBy(i => i.GameId).ToDictionary(g => g.Key, g => g.First().Retry);
        var installed = library.InstalledGameIds(rules).ToHashSet();
        var steamHere = kidHere && SteamRunning();
        _steamSeenSince = steamHere ? _steamSeenSince ?? DateTimeOffset.Now : null;

        lock (_gate)
        {
            foreach (var id in _status.Keys.Where(id => rules.Games.All(g => g.Id != id)).ToList()) { _status.Remove(id); _attempts.Remove(id); }

            foreach (var g in rules.Games)
            {
                var isWanted = wanted.TryGetValue(g.Id, out var retry);
                if (!_attempts.TryGetValue(g.Id, out var at) || at.Retry != retry)
                    _attempts[g.Id] = at = new Attempt { Retry = retry };

                GameInstallStatus st;
                if (g.Launcher == "battlenet") st = BattleNet(g, at, isWanted, kidHere);   // its folder appears long before it is done
                else if (installed.Contains(g.Id)) st = new("installed");
                else if (!isWanted) st = new("not_installed");
                else st = g.Launcher switch
                {
                    "steam" => Steam(g, at, kidHere, steamHere, gameRunning),
                    "direct" => Direct(g, at),
                    "gamepass" => new("manual", "Game Pass games install from the Xbox app on the PC for now."),
                    _ => new("manual", "The hub doesn't know how to install this kind of game."),
                };
                st = st with { Retry = retry };
                if (_status.TryGetValue(g.Id, out var before) && before.State != st.State)
                    log.LogInformation("game {Title}: {From} -> {To} {Detail}", g.Title, before.State, st.State, st.Detail);
                _status[g.Id] = st;
            }

            // One Steam start or restart picks up every game written since. Never while a game is running, never
            // in the middle of another download (Steam would pause it), and not more than once every few minutes.
            var kick = _attempts.Values.Where(a => a.NeedsKick).ToList();
            var steamBusy = rules.Games.Any(g => g.Launcher == "steam" && _status.GetValueOrDefault(g.Id)?.State == "installing");
            if (kick.Count > 0 && kidHere && !gameRunning && !steamBusy && DateTimeOffset.Now - _lastSteamKick > KickGap)
            {
                _lastSteamKick = DateTimeOffset.Now;
                var kind = steamHere ? "steam_restart" : "steam_start";
                _nudges.Add((new Nudge($"{kind}-{DateTimeOffset.Now.ToUnixTimeSeconds()}", kind), DateTimeOffset.Now));
                foreach (var a in kick) { a.NeedsKick = false; a.KickedAt = DateTimeOffset.Now; a.Restarted = steamHere; }
                log.LogInformation("asked the shell to {Kind} so Steam picks up {Count} new game(s)", kind, kick.Count);
            }
        }
    }

    // ------------------------------------------------------------------ Steam

    private GameInstallStatus Steam(Game g, Attempt at, bool kidHere, bool steamHere, bool gameRunning)
    {
        if (LauncherNotReady("steam", "Steam") is { } wait) return wait;
        var m = library.SteamManifest(g.LaunchRef);
        if (m is null)
        {
            // Steam deletes a manifest for a game the signed-in account can't download. GUESS: that is the usual reason.
            if (at.ManifestWritten && at.KickedAt is { } k && DateTimeOffset.Now - k > TimeSpan.FromMinutes(2) && steamHere)
                return new("not_owned", $"Steam turned it down. That usually means {_kidName}'s Steam account doesn't have it yet: buy it or add it (free games too), then press Try again.");
            if (!WriteManifest(g, at)) return new("failed", "Couldn't write to Steam's games folder on this PC.");
            m = library.SteamManifest(g.LaunchRef);
            if (m is null) return new("failed", "Couldn't write to Steam's games folder on this PC.");
        }
        if ((m.Flags & FullyInstalled) != 0 && (m.Flags & UpdateRequired) == 0) return new("installed");

        if (at.LogOffset >= 0 && SteamLogSays(g.LaunchRef, at.LogOffset) is { } said) return said;

        var total = Math.Max(m.BytesToDownload, m.BytesToStage);
        var done = m.BytesToDownload > 0 ? m.BytesDownloaded : m.BytesStaged;
        var free = FreeBytes(cfg.SteamPath);
        if (total > 0 && free is { } f && total - done > f + (512L << 20))
            return new("no_space", $"It needs {Gb(total - done)} more and the PC has {Gb(f)} free. Remove a game he no longer plays, then press Try again.");
        if ((m.Flags & Busy) != 0 || done > 0)
        {
            var (stage, says) = (m.Flags & UpdatePaused) != 0 ? ("Paused", "Paused in Steam. It carries on when Steam is told to, or after Try again.")
                : (m.Flags & Validating) != 0 ? ("Checking files", "Steam is checking the files it has so far.")
                : (m.Flags & Preallocating) != 0 ? ("Making room", "Steam is setting aside space on the drive for it.")
                : (m.Flags & (Staging | Committing)) != 0 ? ("Installing", "Downloaded. Steam is unpacking it.")
                : ("Downloading", "");
            return new("installing", says, done, total, Stage: stage);
        }

        if (!kidHere) return new("waiting", $"Starts when {_kidName} is signed in on the PC. Steam downloads it then, even on the lock screen.", Stage: "Waiting for the PC");
        if (!SteamEverSignedIn())
            return new("needs_signin", $"Steam has never been signed in on {_kidName}'s PC. Sign in once with {_kidName}'s Steam account and tick Remember me. After that, games install by themselves.", Stage: "Needs Steam sign-in");
        if (steamHere && SteamSignedInNow() == false && _steamSeenSince is { } since && DateTimeOffset.Now - since > TimeSpan.FromMinutes(1))
            return new("needs_signin", $"Steam is open on {_kidName}'s PC but signed out. Sign in once with his Steam account and tick Remember me.", Stage: "Needs Steam sign-in");

        if (at.KickedAt is null)
        {
            at.NeedsKick = true;
            return gameRunning
                ? new("waiting", $"Starts after {_kidName} closes his game, so it doesn't interrupt him.", Stage: "After his game")
                : new("waiting", "Handing it to Steam. Steam starts or restarts in the background within a few minutes.", Stage: "Starting Steam");
        }
        var kicked = $"Steam was {(at.Restarted ? "restarted" : "started")} at {at.KickedAt.Value:h:mm tt} to pick it up";
        if (DateTimeOffset.Now - at.KickedAt > StuckAfter)
            return new("stuck", $"{kicked}, but it hasn't downloaded anything since. Try again restarts Steam on the PC. If that doesn't work, open Steam on the PC and press Install on the game.", Stage: "Stuck");
        return new("waiting", $"{kicked}. It hasn't started downloading yet; that usually takes a minute or two.", Stage: "Waiting for Steam");
    }

    [GeneratedRegex("[^A-Za-z0-9 _.-]")]
    private static partial Regex NotForFolder();

    /// <summary>Tells Steam the game should be here and needs downloading. Steam reads manifests when it starts.</summary>
    private bool WriteManifest(Game g, Attempt at)
    {
        try
        {
            Directory.CreateDirectory(library.SteamApps);
            var dir = NotForFolder().Replace(g.Title, "").Trim();
            if (dir == "") dir = g.LaunchRef;
            var acf = $$"""
                "AppState"
                {
                	"appid"		"{{g.LaunchRef}}"
                	"Universe"		"1"
                	"name"		"{{g.Title.Replace("\"", "")}}"
                	"StateFlags"		"1026"
                	"installdir"		"{{dir}}"
                	"LastUpdated"		"0"
                	"SizeOnDisk"		"0"
                	"buildid"		"0"
                	"AutoUpdateBehavior"		"0"
                	"AllowOtherDownloadsWhileRunning"		"0"
                	"ScheduledAutoUpdate"		"0"
                }
                """;
            File.WriteAllText(Path.Combine(library.SteamApps, $"appmanifest_{g.LaunchRef}.acf"), acf.Replace("\n", "\r\n"));
            at.ManifestWritten = true;
            at.LogOffset = SteamLogLength();
            events.Add("game_install_started", $"{g.Title} (Steam)");
            log.LogInformation("wrote Steam manifest for {Title} ({AppId})", g.Title, g.LaunchRef);
            return true;
        }
        catch (Exception ex)
        {
            log.LogWarning("could not write Steam manifest for {AppId}: {Msg}", g.LaunchRef, ex.Message);
            return false;
        }
    }

    private string SteamLog => Path.Combine(cfg.SteamPath, "logs", "content_log.txt");

    private long SteamLogLength() { try { return new FileInfo(SteamLog).Length; } catch { return 0; } }

    [GeneratedRegex(@"\(([^)]+)\)\s*$")]
    private static partial Regex LastBrackets();

    /// <summary>Steam's own log lines about this game since the attempt began. GUESS at the wording, from what Steam
    /// prints when an update can't start, e.g. "AppID 440 update canceled : Failed to start update (No subscription)".</summary>
    private GameInstallStatus? SteamLogSays(string appId, long from)
    {
        try
        {
            using var fs = new FileStream(SteamLog, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            if (fs.Length < from) from = 0;          // Steam started a new log
            fs.Seek(from, SeekOrigin.Begin);
            using var r = new StreamReader(fs);
            GameInstallStatus? said = null;
            while (r.ReadLine() is { } line)
            {
                if (!line.Contains($"AppID {appId} ", StringComparison.OrdinalIgnoreCase)) continue;
                if (Regex.IsMatch(line, "no subscription|no license", RegexOptions.IgnoreCase))
                    said = new("not_owned", $"Steam says {_kidName}'s account doesn't have this game. Buy it or add it on his account (free games too), then press Try again.");
                else if (Regex.IsMatch(line, "disk space|not enough space", RegexOptions.IgnoreCase))
                    said = new("no_space", "Steam says there isn't enough room on the PC. Remove a game he no longer plays, then press Try again.");
                else if (Regex.IsMatch(line, "update canceled|failed to start update", RegexOptions.IgnoreCase))
                    said = new("failed", $"Steam stopped: {LastBrackets().Match(line).Groups[1].Value.Trim()}".TrimEnd(':', ' '));
                else if (Regex.IsMatch(line, "update (started|running)|state changed.*(downloading|update running)", RegexOptions.IgnoreCase))
                    said = null;                      // it got going after all
            }
            return said;
        }
        catch { return null; }
    }

    private static bool SteamRunning()
    {
        var session = Sessions.ConsoleSessionId;
        var procs = Process.GetProcessesByName("steam");
        try { return procs.Any(p => { try { return (uint)p.SessionId == session; } catch { return false; } }); }
        finally { foreach (var p in procs) p.Dispose(); }
    }

    /// <summary>Somebody has signed in to Steam on this PC at least once (Steam remembers them in loginusers.vdf).</summary>
    private bool SteamEverSignedIn()
    {
        try
        {
            var f = Path.Combine(cfg.SteamPath, "config", "loginusers.vdf");
            return File.Exists(f) && File.ReadAllText(f).Contains("\"AccountName\"", StringComparison.OrdinalIgnoreCase);
        }
        catch { return true; }   // can't tell: don't send a parent to the PC for nothing
    }

    /// <summary>Steam in the kid's session is signed in right now (HKCU\Software\Valve\Steam\ActiveProcess\ActiveUser is
    /// his Steam id while signed in, 0 when not). Null when it can't be told.</summary>
    private bool? SteamSignedInNow()
    {
        try
        {
            if (KidSid() is not { } sid) return null;
            using var k = Registry.Users.OpenSubKey($@"{sid}\Software\Valve\Steam\ActiveProcess");
            return k?.GetValue("ActiveUser") is int id ? id != 0 : null;
        }
        catch { return null; }
    }

    // ------------------------------------------------------------------ Battle.net

    private GameInstallStatus BattleNet(Game g, Attempt at, bool wanted, bool kidHere)
    {
        var folder = library.BattleNetFolder(g.LaunchRef);
        if (folder is null) return new("manual", "The hub doesn't know where Battle.net puts this game.");
        var exists = Directory.Exists(folder);
        if (exists && at.KickedAt is null) return new("installed");     // it was there before we did anything
        if (!wanted && !exists) return new("not_installed");

        if (exists)
        {
            // Battle.net says nothing from outside, so watch the folder: growing = installing, quiet for a while = done.
            if (DateTimeOffset.Now - at.SizedAt > TimeSpan.FromSeconds(60))
            {
                at.SizedAt = DateTimeOffset.Now;
                var size = FolderBytes(folder);
                if (size != at.LastSize) { at.LastSize = size; at.SizeSince = DateTimeOffset.Now; }
            }
            if (at.LastSize > 100L << 20 && DateTimeOffset.Now - at.SizeSince > TimeSpan.FromMinutes(5))
            {
                events.Add("game_installed", $"{g.Title} (Battle.net)");
                at.KickedAt = null;          // done: from now on the folder alone says installed
                return new("installed");
            }
            return new("installing", $"Battle.net is downloading it. {Gb(Math.Max(at.LastSize, 0))} on the PC so far.", Math.Max(at.LastSize, 0), 0, Stage: "Downloading");
        }

        if (LauncherNotReady("battlenet", "Battle.net") is { } wait) return wait;
        if (!kidHere) return new("waiting", $"Starts when {_kidName} is signed in on the PC.");
        // Battle.net says nothing about size before it starts, and a game that fills the drive breaks the whole PC.
        // So check against a rough size first (GUESS: current install sizes, rounded up).
        if (BattleNetSizesGb.TryGetValue(g.LaunchRef, out var needGb) && FreeBytes(cfg.BattleNetGamesRoot) is { } freeNow && freeNow < (long)(needGb * 1.1 * (1L << 30)))
            return new("no_space", $"{g.Title} needs about {needGb} GB and the PC has {Gb(freeNow)} free. Remove a game he no longer plays, then press Try again.", 0, (long)needGb << 30);
        if (!BattleNetSignedIn())
            return new("needs_signin", $"Battle.net has to be signed in on {_kidName}'s PC once, with a Blizzard account (yours or his). Tick Keep me logged in. After that, games install by themselves.");
        if (at.KickedAt is null)      // once per attempt; "Try again" on the dashboard starts a new attempt
        {
            at.KickedAt = DateTimeOffset.Now;
            _nudges.Add((new Nudge($"bnet-{g.Id}-{at.Retry}-{DateTimeOffset.Now.ToUnixTimeSeconds()}", "battlenet_install", g.LaunchRef), DateTimeOffset.Now));
            events.Add("game_install_started", $"{g.Title} (Battle.net)");
            return new("waiting", $"Asked Battle.net to install it at {DateTimeOffset.Now:h:mm tt}.", Stage: "Waiting for Battle.net");
        }
        if (DateTimeOffset.Now - at.KickedAt > StuckAfter)
            return new("stuck", "Battle.net didn't start the download. Press Try again. If that doesn't work, open Battle.net on the PC and press Install on the game.");
        return new("waiting", $"Battle.net was asked at {at.KickedAt.Value:h:mm tt} and hasn't started downloading yet.", Stage: "Waiting for Battle.net");
    }

    private static readonly Dictionary<string, int> BattleNetSizesGb = new(StringComparer.OrdinalIgnoreCase)
    {
        ["WoW"] = 100, ["WoWF"] = 70, ["WoWC"] = 60, ["WoWCE"] = 15, ["WoWA"] = 20, ["Pro"] = 50, ["Hero"] = 30, ["WTCG"] = 5, ["Fen"] = 90, ["S2"] = 35, ["S1"] = 2,
        ["W3"] = 30, ["D3"] = 25, ["OSI"] = 30, ["ANBS"] = 10, ["WLBY"] = 40, ["RTRO"] = 2,
    };   // keep in step with BattleNetFolders in GameLibrary.cs

    private string _productsTold = "";

    /// <summary>Tells the parents' activity feed which World of Warcraft versions Battle.net itself knows about on this PC
    /// (its codes and folders, read from its own product.db), so the guessed codes above can be checked against the
    /// real ones. Once per change.</summary>
    private void ReportBattleNetProducts()
    {
        try
        {
            var db = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "Battle.net", "Agent", "product.db");
            if (!File.Exists(db)) return;
            var text = System.Text.Encoding.Latin1.GetString(File.ReadAllBytes(db));
            var found = Regex.Matches(text, @"wow[a-z_]*|World of Warcraft[\\/]_[a-z_]+_", RegexOptions.IgnoreCase)
                .Select(m => m.Value).Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(x => x).Take(20).ToList();
            var line = string.Join(", ", found);
            if (line == "" || line == _productsTold) return;
            _productsTold = line;
            events.Add("battlenet_products", line.Length > 290 ? line[..290] : line);
        }
        catch (Exception ex) { log.LogDebug("product.db: {Msg}", ex.Message); }
    }

    /// <summary>Battle.net remembers who signed in, in the kid's Battle.net.config (SavedAccountNames). GUESS at the file and key.</summary>
    private bool BattleNetSignedIn()
    {
        try
        {
            var f = Path.Combine(KidProfile(), "AppData", "Roaming", "Battle.net", "Battle.net.config");
            if (!File.Exists(f)) return false;
            var m = Regex.Match(File.ReadAllText(f), "\"SavedAccountNames\"\\s*:\\s*\"([^\"]*)\"");
            return !m.Success || m.Groups[1].Value.Trim() != "";
        }
        catch { return true; }
    }

    // ------------------------------------------------------------------ installer share

    private GameInstallStatus Direct(Game g, Attempt at)
    {
        var setup = string.IsNullOrEmpty(cfg.InstallerShare) ? "" : Path.Combine(cfg.InstallerShare, g.Id.ToString(), "setup.exe");
        if (at.Running is null)
        {
            if (setup == "" || !File.Exists(setup))
                return new("manual", $"Put its installer on the home server as {(cfg.InstallerShare == "" ? "<installer folder>" : cfg.InstallerShare)}\\{g.Id}\\setup.exe, then press Try again.");
            at.Running = Task.Run(async () =>
            {
                var args = File.Exists(setup + ".args") ? await File.ReadAllTextAsync(setup + ".args") : "/S";
                using var p = Process.Start(new ProcessStartInfo(setup, args) { UseShellExecute = false, CreateNoWindow = true })!;
                await p.WaitForExitAsync().WaitAsync(TimeSpan.FromMinutes(60));
                return p.ExitCode;
            });
            events.Add("game_install_started", $"{g.Title} (installer)");
        }
        if (!at.Running.IsCompleted) return new("installing", "Running its installer.");
        if (at.Running.IsFaulted) return new("failed", $"Its installer didn't run: {at.Running.Exception?.GetBaseException().Message}");
        return File.Exists(g.LaunchRef) ? new("installed") : new("failed", $"Its installer finished but {Path.GetFileName(g.LaunchRef)} isn't where the hub expects it.");
    }

    // ------------------------------------------------------------------ shared

    /// <summary>The store app itself isn't on the PC yet: the game waits for it (AppInstaller is putting it on).</summary>
    private GameInstallStatus? LauncherNotReady(string app, string name)
    {
        var st = apps.Status.GetValueOrDefault(app);
        if (st?.State == "installed") return null;
        if (app == "steam" && File.Exists(Path.Combine(cfg.SteamPath, "steam.exe")) && st is null) return null;
        if (app == "battlenet" && File.Exists(cfg.BattleNetPath) && st is null) return null;
        return new("waiting", st?.State == "failed" ? $"{name} isn't on this PC and wouldn't install by itself." : $"{name} is being put on this PC first.");
    }

    private string? _sid;
    private string? KidSid()
    {
        if (_sid is not null) return _sid;
        try { return _sid = ((SecurityIdentifier)new NTAccount(cfg.KidUser).Translate(typeof(SecurityIdentifier))).Value; }
        catch { return null; }
    }

    private string KidProfile()
    {
        try
        {
            if (KidSid() is { } sid)
            {
                using var k = Registry.LocalMachine.OpenSubKey($@"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}");
                if (k?.GetValue("ProfileImagePath") is string p) return Environment.ExpandEnvironmentVariables(p);
            }
        }
        catch { /* fall back below */ }
        return Path.Combine(Path.GetPathRoot(Environment.SystemDirectory)!, "Users", cfg.KidUser);
    }

    private static long? FreeBytes(string path)
    {
        try { return new DriveInfo(Path.GetPathRoot(path)!).AvailableFreeSpace; } catch { return null; }
    }

    private static long FolderBytes(string folder)
    {
        try
        {
            return new DirectoryInfo(folder).EnumerateFiles("*", new EnumerationOptions { RecurseSubdirectories = true, IgnoreInaccessible = true })
                .Sum(f => { try { return f.Length; } catch { return 0L; } });
        }
        catch { return 0; }
    }

    private static string Gb(long bytes) => $"{bytes / (double)(1L << 30):0.#} GB";
}
