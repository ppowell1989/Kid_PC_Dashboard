using System.Net.Http.Json;
using Microsoft.Data.Sqlite;
using Microsoft.Win32;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>
/// Reads the kid's Chrome history (pages and search terms) every couple of minutes and sends what's
/// new to the hub. This gives parents the web activity view without needing the Chrome extension.
/// Chrome keeps the History file open, so we read a copy.
/// </summary>
public sealed class ChromeHistory(HttpClient http, DeviceConfig cfg, ILogger<ChromeHistory> log) : BackgroundService
{
    private static readonly string StateFile = Path.Combine(DeviceConfig.Dir, "history-since.txt");
    private static readonly HashSet<string> Skip = new(StringComparer.OrdinalIgnoreCase) { "chrome", "chrome-extension", "about", "file", "devtools" };

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        await Task.Delay(TimeSpan.FromSeconds(20), ct);
        while (!ct.IsCancellationRequested)
        {
            try { await SyncAsync(ct); }
            catch (Exception ex) when (ex is not OperationCanceledException) { log.LogWarning("history sync failed: {Msg}", ex.Message); }
            await Task.Delay(TimeSpan.FromSeconds(cfg.HistorySeconds), ct);
        }
    }

    private async Task SyncAsync(CancellationToken ct)
    {
        var since = ReadSince();
        var newest = since;
        var visits = new List<object>();
        var searches = new List<object>();
        foreach (var db in HistoryFiles())
        {
            var copy = Path.Combine(Path.GetTempPath(), $"kidspc-history-{Guid.NewGuid():N}.db");
            try
            {
                using (var src = new FileStream(db, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var dst = File.Create(copy))
                    src.CopyTo(dst);
                using var conn = new SqliteConnection($"Data Source={copy};Mode=ReadOnly;Pooling=False");
                conn.Open();

                using (var cmd = conn.CreateCommand())
                {
                    cmd.CommandText = "SELECT u.url, u.title, v.visit_time FROM visits v JOIN urls u ON u.id = v.url WHERE v.visit_time > $s ORDER BY v.visit_time LIMIT 500";
                    cmd.Parameters.AddWithValue("$s", since);
                    using var r = cmd.ExecuteReader();
                    while (r.Read())
                    {
                        var url = r.GetString(0);
                        var t = r.GetInt64(2);
                        newest = Math.Max(newest, t);
                        if (Uri.TryCreate(url, UriKind.Absolute, out var u) && Skip.Contains(u.Scheme)) continue;
                        visits.Add(new { url = url.Length > 2000 ? url[..2000] : url, title = r.IsDBNull(1) ? "" : Trim(r.GetString(1), 300), at = When(t) });
                    }
                }
                using (var cmd = conn.CreateCommand())
                {
                    cmd.CommandText = "SELECT k.term, u.last_visit_time FROM keyword_search_terms k JOIN urls u ON u.id = k.url_id WHERE u.last_visit_time > $s LIMIT 500";
                    cmd.Parameters.AddWithValue("$s", since);
                    using var r = cmd.ExecuteReader();
                    while (r.Read()) searches.Add(new { term = Trim(r.GetString(0), 300), at = When(r.GetInt64(1)) });
                }
            }
            finally
            {
                SqliteConnection.ClearAllPools();
                try { File.Delete(copy); } catch { /* temp */ }
            }
        }
        if (visits.Count == 0 && searches.Count == 0) { if (newest > since) WriteSince(newest); return; }

        var res = await http.PostAsJsonAsync("/api/device/history", new { visits, searches }, ct);
        res.EnsureSuccessStatusCode();   // don't advance on failure: resend next time
        WriteSince(newest);
        log.LogInformation("sent {V} pages and {S} searches", visits.Count, searches.Count);
    }

    private IEnumerable<string> HistoryFiles()
    {
        var home = KidProfileDir();
        if (home is null) yield break;
        var root = Path.Combine(home, "AppData", "Local", "Google", "Chrome", "User Data");
        if (!Directory.Exists(root)) yield break;
        foreach (var dir in Directory.EnumerateDirectories(root))
        {
            var name = Path.GetFileName(dir);
            if (name != "Default" && !name.StartsWith("Profile ")) continue;
            var f = Path.Combine(dir, "History");
            if (File.Exists(f)) yield return f;
        }
    }

    /// <summary>The kid's profile folder, from the ProfileList (it isn't always C:\Users\name).</summary>
    private string? KidProfileDir()
    {
        if (cfg.KidUser.Length == 0) return null;
        using var list = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList");
        foreach (var sid in list?.GetSubKeyNames() ?? [])
        {
            using var k = list!.OpenSubKey(sid);
            if (k?.GetValue("ProfileImagePath") is string p && Path.GetFileName(p).Equals(cfg.KidUser, StringComparison.OrdinalIgnoreCase))
                return Environment.ExpandEnvironmentVariables(p);
        }
        var guess = Path.Combine(Path.GetPathRoot(Environment.SystemDirectory)!, "Users", cfg.KidUser);
        return Directory.Exists(guess) ? guess : null;
    }

    // Chrome times are microseconds since 1601-01-01 UTC.
    private static string When(long chromeTime) =>
        new DateTimeOffset(DateTime.FromFileTimeUtc(chromeTime * 10)).ToLocalTime().ToString("yyyy-MM-ddTHH:mm:sszzz");

    private static string Trim(string s, int n) => s.Length > n ? s[..n] : s;

    private static long ReadSince()
    {
        try { return long.Parse(File.ReadAllText(StateFile)); }
        catch { return (DateTime.UtcNow.AddDays(-1).ToFileTimeUtc()) / 10; }   // first run: the last day
    }

    private static void WriteSince(long t) => File.WriteAllText(StateFile, t.ToString());
}
