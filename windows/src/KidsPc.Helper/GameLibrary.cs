using System.Management;
using System.Text.RegularExpressions;
using KidsPc.Shared;

namespace KidsPc.Helper;

public sealed record Activity(string Title, bool Active, List<GameProcess> Processes);

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
    private static readonly Dictionary<string, string> BattleNetFolders = new(StringComparer.OrdinalIgnoreCase)
    {
        ["WoW"] = "World of Warcraft",
        ["Pro"] = "Overwatch",
        ["Hero"] = "Heroes of the Storm",
        ["WTCG"] = "Hearthstone",
        ["Fen"] = "Diablo IV",
    };

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

    public List<int> InstalledGameIds(Rules? rules)
    {
        if (rules is null) return [];
        var steamIds = SteamInstalls().Values.Select(v => v.AppId).ToHashSet();
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
            next:;
        }

        var game = found.FirstOrDefault(p => p.Kind != "chrome");
        var title = game?.Approved?.Title
                    ?? (game?.SteamAppId is { } id ? steam.Values.FirstOrDefault(v => v.AppId == id).Name : null)
                    ?? (found.Any(p => p.Kind == "chrome") ? "Chrome" : "");
        return new Activity(title ?? "", found.Count > 0, found);
    }
}
