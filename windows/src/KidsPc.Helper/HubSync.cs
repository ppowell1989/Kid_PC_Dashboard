using System.Net.Http.Json;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>Last good rules from the hub, plus a fallback for when the hub is unreachable.</summary>
public sealed class StateCache
{
    private readonly object _gate = new();
    private Rules? _rules;
    private DateTimeOffset _fetchedAt;
    private bool _reachable;
    private bool _locked = true;
    private readonly List<ParentMessage> _messages = [];
    private string _voice = "";

    /// <summary>Which voice room the shell has open ("", "family" or "friends"), reported to the hub for the Family row.</summary>
    public string Voice { get { lock (_gate) return _voice; } set { lock (_gate) _voice = value; } }

    public void AddMessage(ParentMessage m) { lock (_gate) { _messages.Add(m); if (_messages.Count > 5) _messages.RemoveAt(0); } }

    // The lock comes with the rules. Setting it later left a gap after every start-up and sign-in in which the
    // shell had today's rules but still the "locked" this cache starts with, and showed "time's up" with time left.
    public void Set(Rules rules) { lock (_gate) { _rules = rules; _fetchedAt = DateTimeOffset.Now; _reachable = true; _locked = rules.Time.Locked; } }
    public void MarkUnreachable() { lock (_gate) _reachable = false; }
    public void SetLocked(bool locked) { lock (_gate) _locked = locked; }

    public Rules? Rules { get { lock (_gate) return _rules; } }

    public ShellState Snapshot(IEnumerable<int> installed, List<Nudge>? nudges = null, Dictionary<int, string>? installing = null,
        Dictionary<int, int>? percent = null, List<string>? signIn = null)
    {
        lock (_gate)
        {
            _messages.RemoveAll(m => DateTimeOffset.Now - m.At > TimeSpan.FromMinutes(30));
            return new ShellState(_rules, installed.ToList(), _locked, _reachable, _fetchedAt, [.. _messages], nudges, installing, percent, signIn);
        }
    }

    /// <summary>
    /// Offline policy. GUESS: if the hub has been unreachable for over 15 minutes, keep counting down
    /// from the last known minutes_left locally and lock at zero, rather than failing open.
    /// </summary>
    public bool OfflineLocked(int minutesSinceFetch)
    {
        lock (_gate)
        {
            if (_rules is null) return true;               // never heard from the hub: stay locked
            return _rules.Time.MinutesLeft - minutesSinceFetch <= 0;
        }
    }

    public DateTimeOffset FetchedAt { get { lock (_gate) return _fetchedAt; } }
}

/// <summary>Every PollSeconds: report what is running, pull rules, enforce them.</summary>
public sealed class HubSync(HttpClient http, DeviceConfig cfg, StateCache cache, GameLibrary library, Enforcer enforcer, ChromePolicy chrome, AppInstaller apps, EventQueue events, CommandRunner commands, Updater updater, NetworkFilter dns, GameDeployer deployer, KidLockdown lockdown, TimeZoneSync clock, ILogger<HubSync> log)
    : BackgroundService
{
    // Play the hub never heard about, because it couldn't be reached (network cable out, Wi-Fi off, hub down).
    // Kept on disk so a restart doesn't forget it, and handed to the hub with the next heartbeat that gets
    // through. Without this, unplugging the network was free play time: the PC locked itself once the
    // minutes it knew about ran out, but the hub had counted none of them and unlocked it again.
    private static readonly string OfflineFile = Path.Combine(DeviceConfig.Dir, "offline-seconds.txt");
    private double _offlineSeconds = ReadOffline();

    private static double ReadOffline()
    {
        try { return Math.Clamp(double.Parse(File.ReadAllText(OfflineFile), System.Globalization.CultureInfo.InvariantCulture), 0, 24 * 3600); }
        catch { return 0; }
    }

    private void SaveOffline()
    {
        try { File.WriteAllText(OfflineFile, _offlineSeconds.ToString("F0", System.Globalization.CultureInfo.InvariantCulture)); }
        catch (Exception ex) { log.LogWarning("could not save offline play time: {Msg}", ex.Message); }
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var lastLoop = DateTimeOffset.UtcNow;
        while (!ct.IsCancellationRequested)
        {
            // Time since the last look, capped so a PC that slept doesn't count the nap as play.
            var elapsed = Math.Clamp((DateTimeOffset.UtcNow - lastLoop).TotalSeconds, 0, 60);
            lastLoop = DateTimeOffset.UtcNow;
            var kidHere = Sessions.KidIsAtConsole(cfg.KidUser);
            var activity = kidHere ? library.CurrentActivity(cache.Rules, Sessions.ConsoleSessionId) : new Activity("", false, []);
            try
            {
                await events.FlushAsync(http, ct);
                var appStates = apps.Status.ToDictionary(kv => kv.Key, kv => new { state = kv.Value.State, detail = kv.Value.Detail });
                appStates["kidspc"] = new { state = updater.Status.State, detail = updater.Status.Detail };
                appStates["webfilter"] = new { state = dns.Status.State, detail = dns.Status.Detail };
                var owed = (int)(_offlineSeconds / 60);
                var games = deployer.Status.ToDictionary(kv => kv.Key.ToString(), kv => new { state = kv.Value.State, detail = kv.Value.Detail, done_bytes = kv.Value.DoneBytes, total_bytes = kv.Value.TotalBytes, retry = kv.Value.Retry, stage = kv.Value.Stage, store_log = kv.Value.StoreLog });
                var hb = await http.PostAsJsonAsync("/api/device/heartbeat", new { running = activity.Title, active = activity.Active, apps = appStates, game_id = activity.GameId, voice = kidHere ? cache.Voice : "", offline_minutes = owed, games, disk_free_gb = deployer.FreeGb(), computer = Environment.MachineName,
                    steam_account = deployer.SteamAccountHere() is { } sa ? new { id = sa.Id, name = sa.Name, login = sa.Login } : null }, ct);
                if (owed > 0 && hb.IsSuccessStatusCode)
                {
                    log.LogInformation("told the hub about {Minutes} minutes played while it was unreachable", owed);
                    _offlineSeconds -= owed * 60;
                    SaveOffline();
                }
                var rules = await http.GetFromJsonAsync<Rules>("/api/device/rules", Json.Options, ct);
                if (rules is not null)
                {
                    cache.Set(rules);
                    chrome.Apply(rules.Web);
                    dns.Apply(rules.Web.Dns);
                    clock.Apply(rules.Timezone);
                    apps.Ensure(rules.ManagedApps ?? ["steam", "chrome"]);
                    // Approved games go on by themselves. A game the kid is playing is never interrupted for it.
                    try { deployer.Tick(rules, kidHere, activity.Processes.Any(p => p.Kind is "steam" or "battlenet" or "direct")); }
                    catch (Exception ex) { log.LogWarning("game installs: {Msg}", ex.Message); }
                    await commands.RunAsync(rules.Commands ?? [], ct);
                }
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                cache.MarkUnreachable();
                log.LogWarning("hub unreachable: {Message}", ex.Message);
                if (activity.Active)
                {
                    _offlineSeconds = Math.Min(_offlineSeconds + elapsed, 24 * 3600);
                    SaveOffline();
                }
            }

            var rulesNow = cache.Rules;
            var minutesStale = (int)(DateTimeOffset.Now - cache.FetchedAt).TotalMinutes;
            var locked = rulesNow is null
                ? true
                : minutesStale > 15 ? cache.OfflineLocked(minutesStale) : rulesNow.Time.Locked;

            cache.SetLocked(locked);
            if (kidHere) enforcer.Enforce(locked, activity);
            lockdown.Tick(kidHere, Sessions.ConsoleSessionId, updater.Status.State == "installing");
            await Task.Delay(TimeSpan.FromSeconds(cfg.PollSeconds), ct);
        }
    }
}
