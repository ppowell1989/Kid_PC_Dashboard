using System.Management;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>GameId is the catalogue id of the running game (null for unapproved games and Chrome), so a brother can join it.</summary>
public sealed record Activity(string Title, bool Active, List<GameProcess> Processes, int? GameId = null);

/// <summary>Steam's appmanifest numbers for one game. Flags is StateFlags.</summary>
public sealed record SteamManifest(string AppId, long Flags, long BytesToDownload, long BytesDownloaded, long BytesToStage, long BytesStaged, string File, string InstallDir = "");

/// <summary>A running process the helper recognises, and the catalogue game it belongs to (if any).</summary>
/// Name is the store's own title for an unapproved game, where the store has one (Epic's install record).
public sealed record GameProcess(int Pid, string Path, string Kind, string? SteamAppId, Game? Approved, string? Name = null);

/// <summary>One game in the Epic launcher's install records. Complete is false while it is still downloading.</summary>
public sealed record EpicInstall(string AppName, string Title, string Folder, bool Complete, long Size, string File);

/// <summary>A game on the Epic account signed in on this PC, from the launcher's library cache.</summary>
public sealed record EpicOwned(string AppName, string Title, string ImageUrl);

/// <summary>
/// Knows what is installed and what is running.
/// Steam: reads steamapps\appmanifest_*.acf to map install folders to app ids.
/// Battle.net: GUESS, matches catalogue launch_ref to a folder under BattleNetGamesRoot by a small table.
/// Epic: reads the launcher's .item install records (one JSON file per game) for each game's folder and app name.
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
        "SystemSettings.exe", "control.exe",    // Windows Settings (the gear in Quick Settings, Win+I) and Control Panel
    };

    private static readonly Dictionary<string, string> BattleNetFolders = new(StringComparer.OrdinalIgnoreCase)
    {
        // World of Warcraft's versions share one folder, each in its own subfolder. The version a parent approved
        // is the one installed and launched; another version being on the PC doesn't count.
        ["WoW"] = @"World of Warcraft\_retail_",
        ["WoWF"] = @"World of Warcraft\_forever_",   // GUESS from fan sites: Blizzard hasn't published the folder
        ["WoWC"] = @"World of Warcraft\_classic_",        // Mists of Pandaria Classic (the Classic progression client)
        ["WoWCE"] = @"World of Warcraft\_classic_era_",   // Classic Era, Hardcore, Season of Discovery
        ["WoWA"] = @"World of Warcraft\_anniversary_",    // Burning Crusade Classic Anniversary. GUESS: folder name
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

    // ------------------------------------------------------------------ Epic

    /// <summary>Every game in the Epic launcher's install records, finished or not. GUESS at the fields: AppName,
    /// DisplayName, InstallLocation, InstallSize, bIsIncompleteInstall (what the launcher has written for years).</summary>
    public List<EpicInstall> EpicInstalls()
    {
        var found = new Dictionary<string, EpicInstall>(StringComparer.OrdinalIgnoreCase);
        foreach (var (dir, pending) in new[] { (Epic.Manifests, false), (Path.Combine(Epic.Manifests, "Pending"), true) })
        {
            if (!Directory.Exists(dir)) continue;
            foreach (var f in Directory.EnumerateFiles(dir, "*.item"))
            {
                try
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(f));
                    var r = doc.RootElement;
                    string S(string k) => r.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
                    var app = S("AppName");
                    var folder = S("InstallLocation");
                    if (app == "" || folder == "" || found.ContainsKey(app)) continue;   // the finished record (read first) wins over a pending one
                    var incomplete = pending || (r.TryGetProperty("bIsIncompleteInstall", out var inc) && inc.ValueKind == JsonValueKind.True);
                    var size = r.TryGetProperty("InstallSize", out var sz) && sz.ValueKind == JsonValueKind.Number && sz.TryGetInt64(out var n) ? n : 0;
                    found[app] = new(app, S("DisplayName") is { Length: > 0 } t ? t : app, folder.Replace('/', '\\').TrimEnd('\\'),
                        !incomplete && Directory.Exists(folder), size, f);
                }
                catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
                {
                    log.LogDebug("skip {Item}: {Msg}", f, ex.Message);
                }
            }
        }
        return found.Values.ToList();
    }

    /// <summary>The Epic game a running program belongs to: (app name, title) from its install record, or (null, null)
    /// for something under the Epic games folder that has no record yet. Null when it isn't an Epic game at all.
    /// The launcher's own programs are never a game.</summary>
    private (string? AppName, string? Title)? EpicGameAt(string path, List<EpicInstall> epic)
    {
        if (path.StartsWith(Epic.LauncherFolder + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) return null;
        foreach (var e in epic)
            if (path.StartsWith(e.Folder + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) return (e.AppName, e.Title);
        var root = cfg.EpicGamesRoot.TrimEnd('\\');
        if (root != "" && path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
            && !path.StartsWith(Path.Combine(root, "Launcher") + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
            && !path.Contains(@"\Epic Online Services\", StringComparison.OrdinalIgnoreCase))
            return (null, null);
        return null;
    }

    /// <summary>The games on the Epic account signed in on this PC, from the launcher's library cache (catcache.bin:
    /// base64 of a JSON list of catalogue items). Add-ons are left out. GUESS at the format, as Playnite reads it.
    /// Empty when the launcher hasn't been signed in or the file can't be read.</summary>
    public List<EpicOwned> EpicLibrary()
    {
        var list = new List<EpicOwned>();
        try
        {
            if (!File.Exists(Epic.CatalogCache)) return list;
            var json = Encoding.UTF8.GetString(Convert.FromBase64String(File.ReadAllText(Epic.CatalogCache).Trim()));
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind != JsonValueKind.Array) return list;
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var item in doc.RootElement.EnumerateArray())
            {
                if (item.TryGetProperty("mainGameItem", out var main) && main.ValueKind == JsonValueKind.Object) continue;   // DLC
                var title = item.TryGetProperty("title", out var t) ? t.GetString() ?? "" : "";
                string? app = null;
                if (item.TryGetProperty("releaseInfo", out var rel) && rel.ValueKind == JsonValueKind.Array)
                    foreach (var ri in rel.EnumerateArray())
                        if (ri.TryGetProperty("appId", out var a) && a.GetString() is { Length: > 0 } s) { app = s; break; }
                if (app is null || title == "" || !Epic.IsAppName(app) || !seen.Add(app)) continue;
                if (item.TryGetProperty("categories", out var cats) && cats.ValueKind == JsonValueKind.Array
                    && cats.EnumerateArray().Any(c => c.TryGetProperty("path", out var p) && p.GetString() is "addons" or "digitalextras" or "engines" or "assets"))
                    continue;
                var image = "";
                if (item.TryGetProperty("keyImages", out var imgs) && imgs.ValueKind == JsonValueKind.Array)
                {
                    var byType = imgs.EnumerateArray()
                        .Where(i => i.TryGetProperty("type", out _) && i.TryGetProperty("url", out _))
                        .GroupBy(i => i.GetProperty("type").GetString() ?? "")
                        .ToDictionary(g => g.Key, g => g.First().GetProperty("url").GetString() ?? "");
                    image = new[] { "DieselGameBox", "OfferImageWide", "DieselStoreFrontWide", "DieselGameBoxTall", "Thumbnail" }
                        .Select(k => byType.GetValueOrDefault(k)).FirstOrDefault(u => !string.IsNullOrEmpty(u)) ?? "";
                }
                list.Add(new(app, title.Length > 120 ? title[..120] : title, image.StartsWith("https://") && image.Length < 500 ? image : ""));
                if (list.Count >= 300) break;
            }
        }
        catch (Exception ex) { log.LogDebug("Epic library: {Msg}", ex.Message); }
        return list;
    }

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
            return new(appId, N("StateFlags"), N("BytesToDownload"), N("BytesDownloaded"), N("BytesToStage"), N("BytesStaged"), file,
                f.GetValueOrDefault("installdir") ?? "");
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
        var epicDone = EpicInstalls().Where(e => e.Complete).Select(e => e.AppName).ToHashSet(StringComparer.OrdinalIgnoreCase);
        return rules.Games.Where(g => g.Launcher switch
        {
            "steam" => steamIds.Contains(g.LaunchRef),
            "epic" => epicDone.Contains(g.LaunchRef),
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
        var epic = EpicInstalls();
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
            if (EpicGameAt(path, epic) is { } ep)
            {
                // Anything under an Epic game's folder, or the Epic games folder itself, is an Epic game. One that isn't
                // approved (a free game he picked up in the Epic store) is closed like an unapproved Steam game.
                found.Add(new(pid, path, "epic", null, ep.AppName is { } an ? approved.FirstOrDefault(g => g.Launcher == "epic" && g.LaunchRef.Equals(an, StringComparison.OrdinalIgnoreCase)) : null, ep.Title));
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
                    ?? game?.Name
                    ?? (game?.SteamAppId is { } id ? steam.Values.FirstOrDefault(v => v.AppId == id).Name : null)
                    ?? (found.Any(p => p.Kind == "chrome") ? "Chrome" : "");
        return new Activity(title ?? "", found.Any(p => p.Kind != "tool"), found, game?.Approved?.Id);
    }
}
