using System.Management;
using System.Text.RegularExpressions;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>GameId is the catalogue id of the running game (null for unapproved games and Chrome), so a brother can join it.</summary>
public sealed record Activity(string Title, bool Active, List<GameProcess> Processes, int? GameId = null);

/// <summary>Steam's appmanifest numbers for one game. Flags is StateFlags.</summary>
public sealed record SteamManifest(string AppId, long Flags, long BytesToDownload, long BytesDownloaded, long BytesToStage, long BytesStaged, string File);

/// <summary>A running process the helper recognises, and the catalogue game it belongs to (if any).</summary>
public sealed record GameProcess(int Pid, string Path, string Kind, string? SteamAppId, Game? Approved);

/// <summary>
/// Knows what is installed and what is running.
/// Steam: reads steamapps\appmanifest_*.acf to map install folders to app ids.
/// Battle.net: GUESS, matches catalogue launch_ref to a folder under BattleNetGamesRoot by a small table.
/// </summary>
public sealed partial class GameLibrary(DeviceConfig cfg, ILogger<GameLibrary> log)
{
    // Battle.net product codes (as used by Battle.net.exe --exec="launch X") to install folder names.
    // GUESS: extend as you add games to the catalogue.
    // msedgewebview2.exe (the shell's voice window) is deliberately not on this list.
    private static readonly HashSet<string> OtherBrowsers = new(StringComparer.OrdinalIgnoreCase)
        { "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "vivaldi.exe", "iexplore.exe", "waterfox.exe", "librewolf.exe", "tor.exe" };

    // Windows' own tools that turn a locked-down account into an open one: a command line, the registry,
    // Task Manager (which can end the kid shell), a file window (which can start anything), and downloaders.
    // The kid's screen offers none of them, but a file picker in Chrome or Steam can start a program by
    // right-click > Open. Closed on sight in the kid's session only; a parent's own sign-in is never touched.
    // This is a stopgap until an allow-list (App Control / AppLocker) is in place.
    private static readonly HashSet<string> EscapeTools = new(StringComparer.OrdinalIgnoreCase)
    {
        "powershell.exe", "pwsh.exe", "powershell_ise.exe", "cmd.exe", "WindowsTerminal.exe", "wt.exe",
        "regedit.exe", "reg.exe", "taskmgr.exe", "mmc.exe", "explorer.exe",
        "wscript.exe", "cscript.exe", "mshta.exe", "curl.exe", "bitsadmin.exe", "certutil.exe",
    };

    private static readonly Dictionary<string, string> BattleNetFolders = new(StringComparer.OrdinalIgnoreCase)
    {
        // World of Warcraft's versions share one folder, each in its own subfolder. The version a parent approved
        // is the one installed and launched; another version being on the PC doesn't count.
        ["WoW"] = @"World of Warcraft\_retail_",
        ["WoWF"] = @"World of Warcraft\_forever_",   // GUESS from fan sites: Blizzard hasn't published the folder
        ["Pro"] = "Overwatch",
        ["Hero"] = "Heroes of the Storm",
        ["WTCG"] = "Hearthstone",
        ["Fen"] = "Diablo IV",
        ["S2"] = "StarCraft II",
        ["S1"] = "StarCraft",
        ["W3"] = "Warcraft III",
        ["D3"] = "Diablo III",
        ["OSI"] = "Diablo II Resurrected",
        ["ANBS"] = "Diablo Immortal",
        ["WLBY"] = "Crash Bandicoot 4",
        ["RTRO"] = "Blizzard Arcade Collection",
    };   // keep in step with BATTLENET in hub/app/gamesearch.py

    [GeneratedRegex("\"(appid|installdir|name)\"\\s+\"([^\"]*)\"", RegexOptions.IgnoreCase)]
    private static partial Regex AcfField();

    /// <summary>installdir (lowercase) -> (appid, name) for every Steam library folder.</summary>
    public Dictionary<string, (string AppId, string Name)> SteamInstalls()
    {
        var result = new Dictionary<string, (string, string)>(StringComparer.OrdinalIgnoreCase);
        foreach (var lib in SteamLibraryFolders())
        {
            var apps = Path.Combine(lib, "steamapps");
            if (!Directory.Exists(apps)) continue;
            foreach (var acf in Directory.EnumerateFiles(apps, "appmanifest_*.acf"))
            {
                try
                {
                    var fields = AcfField().Matches(File.ReadAllText(acf))
                        .GroupBy(m => m.Groups[1].Value.ToLowerInvariant())
                        .ToDictionary(g => g.Key, g => g.First().Groups[2].Value);
                    if (fields.TryGetValue("installdir", out var dir) && fields.TryGetValue("appid", out var id))
                        result[Path.Combine(apps, "common", dir)] = (id, fields.GetValueOrDefault("name", dir));
                }
                catch (IOException ex) { log.LogDebug("skip {Acf}: {Msg}", acf, ex.Message); }
            }
        }
        return result;
    }

    private IEnumerable<string> SteamLibraryFolders()
    {
        yield return cfg.SteamPath;
        var vdf = Path.Combine(cfg.SteamPath, "steamapps", "libraryfolders.vdf");
        if (!File.Exists(vdf)) yield break;
        foreach (Match m in Regex.Matches(File.ReadAllText(vdf), "\"path\"\\s+\"([^\"]+)\""))
        {
            var p = m.Groups[1].Value.Replace(@"\\", @"\");
            if (!p.Equals(cfg.SteamPath, StringComparison.OrdinalIgnoreCase)) yield return p;
        }
    }

    /// <summary>Where Battle.net puts a game, or null for a product code this PC doesn't know.</summary>
    public string? BattleNetFolder(string code) =>
        BattleNetFolders.TryGetValue(code, out var f) ? Path.Combine(cfg.BattleNetGamesRoot, f) : null;

    public string SteamApps => Path.Combine(cfg.SteamPath, "steamapps");

    [GeneratedRegex("^\\s*\"(\\w+)\"\\s+\"([^\"]*)\"", RegexOptions.Multiline)]
    private static partial Regex AcfAny();

    /// <summary>Steam's own record of a game in the main library (steamapps\appmanifest_ID.acf): how installed it is
    /// and how much is downloaded. Null when Steam has no record of it. Top-level fields only.</summary>
    public SteamManifest? SteamManifest(string appId)
    {
        var file = Path.Combine(SteamApps, $"appmanifest_{appId}.acf");
        try
        {
            if (!File.Exists(file)) return null;
            var f = AcfAny().Matches(File.ReadAllText(file))
                .GroupBy(m => m.Groups[1].Value, StringComparer.OrdinalIgnoreCase)
                .ToDictionary(g => g.Key, g => g.First().Groups[2].Value, StringComparer.OrdinalIgnoreCase);
            long N(string k) => long.TryParse(f.GetValueOrDefault(k), out var v) ? v : 0;
            return new(appId, N("StateFlags"), N("BytesToDownload"), N("BytesDownloaded"), N("BytesToStage"), N("BytesStaged"), file);
        }
        catch (IOException) { return null; }     // Steam is writing it; look again next time
    }

    // Steam's StateFlags: 4 = fully installed. A manifest the helper wrote for a game Steam hasn't fetched yet
    // has 1026 (update required + update started) and must not make the game look installed.
    private const long SteamFullyInstalled = 4;

    public List<int> InstalledGameIds(Rules? rules)
    {
        if (rules is null) return [];
        var steamIds = SteamInstalls().Values.Select(v => v.AppId)
            .Where(id => SteamManifest(id) is not { } m || (m.Flags & SteamFullyInstalled) != 0).ToHashSet();
        return rules.Games.Where(g => g.Launcher switch
        {
            "steam" => steamIds.Contains(g.LaunchRef),
            "battlenet" => BattleNetFolders.TryGetValue(g.LaunchRef, out var f) && Directory.Exists(Path.Combine(cfg.BattleNetGamesRoot, f)),
            "direct" => File.Exists(g.LaunchRef),
            "gamepass" => true,   // TODO: query installed packages (PackageManager.FindPackagesForUser) for the kid's SID
            _ => false,
        }).Select(g => g.Id).ToList();
    }

    /// <summary>Recognised game/browser processes in interactive sessions, matched against approvals.</summary>
    // Steam tools that live under steamapps\common but are not games. Never stopped.
    private static readonly HashSet<string> SteamToolAppIds = ["228980" /* Steamworks Common Redistributables */, "1070560", "1391110", "1628350" /* Steam Linux runtimes */];

    public Activity CurrentActivity(Rules? rules, uint sessionId)
    {
        var steam = SteamInstalls();
        var approved = rules?.Games ?? [];
        var found = new List<GameProcess>();

        // WMI gives us ExecutablePath for processes in other sessions, which Process.MainModule cannot from session 0 reliably.
        using var searcher = new ManagementObjectSearcher($"SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE SessionId = {sessionId}");
        foreach (ManagementObject mo in searcher.Get())
        {
            var path = mo["ExecutablePath"] as string;
            if (string.IsNullOrEmpty(path)) continue;
            var pid = Convert.ToInt32(mo["ProcessId"]);

            var steamHit = steam.FirstOrDefault(kv => path.StartsWith(kv.Key + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase));
            if (steamHit.Key is not null)
            {
                var appId = steamHit.Value.AppId;
                if (SteamToolAppIds.Contains(appId)) continue;
                found.Add(new(pid, path, "steam", appId, approved.FirstOrDefault(g => g.Launcher == "steam" && g.LaunchRef == appId)));
                continue;
            }
            foreach (var (code, folder) in BattleNetFolders)
            {
                if (path.StartsWith(Path.Combine(cfg.BattleNetGamesRoot, folder) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                {
                    found.Add(new(pid, path, "battlenet", null, approved.FirstOrDefault(g => g.Launcher == "battlenet" && g.LaunchRef.Equals(code, StringComparison.OrdinalIgnoreCase))));
                    goto next;
                }
            }
            var direct = approved.FirstOrDefault(g => g.Launcher == "direct" && g.LaunchRef.Equals(path, StringComparison.OrdinalIgnoreCase));
            if (direct is not null) { found.Add(new(pid, path, "direct", null, direct)); continue; }
            if (Path.GetFileName(path).Equals("chrome.exe", StringComparison.OrdinalIgnoreCase))
                found.Add(new(pid, path, "chrome", null, null));
            else if (OtherBrowsers.Contains(Path.GetFileName(path)))
                found.Add(new(pid, path, "browser", null, null));   // Chrome is the managed browser; others skip its policies
            else if (EscapeTools.Contains(Path.GetFileName(path)))
                found.Add(new(pid, path, "tool", null, null));
            next:;
        }

        var game = found.FirstOrDefault(p => p.Kind is not ("chrome" or "browser" or "tool"));
        var title = game?.Approved?.Title
                    ?? (game?.SteamAppId is { } id ? steam.Values.FirstOrDefault(v => v.AppId == id).Name : null)
                    ?? (found.Any(p => p.Kind == "chrome") ? "Chrome" : "");
        return new Activity(title ?? "", found.Any(p => p.Kind != "tool"), found, game?.Approved?.Id);
    }
}
