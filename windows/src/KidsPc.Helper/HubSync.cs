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

    public void Set(Rules rules) { lock (_gate) { _rules = rules; _fetchedAt = DateTimeOffset.Now; _reachable = true; } }
    public void MarkUnreachable() { lock (_gate) _reachable = false; }
    public void SetLocked(bool locked) { lock (_gate) _locked = locked; }

    public Rules? Rules { get { lock (_gate) return _rules; } }

    public ShellState Snapshot(IEnumerable<int> installed)
    {
        lock (_gate) return new ShellState(_rules, installed.ToList(), _locked, _reachable, _fetchedAt);
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
public sealed class HubSync(HttpClient http, DeviceConfig cfg, StateCache cache, GameLibrary library, Enforcer enforcer, ChromePolicy chrome, ILogger<HubSync> log)
    : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            var kidHere = Sessions.KidIsAtConsole(cfg.KidUser);
            var activity = kidHere ? library.CurrentActivity(cache.Rules, Sessions.ConsoleSessionId) : new Activity("", false, []);
            try
            {
                await http.PostAsJsonAsync("/api/device/heartbeat", new { running = activity.Title, active = activity.Active }, ct);
                var rules = await http.GetFromJsonAsync<Rules>("/api/device/rules", Json.Options, ct);
                if (rules is not null)
                {
                    cache.Set(rules);
                    chrome.Apply(rules.Web);
                }
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                cache.MarkUnreachable();
                log.LogWarning("hub unreachable: {Message}", ex.Message);
            }

            var rulesNow = cache.Rules;
            var minutesStale = (int)(DateTimeOffset.Now - cache.FetchedAt).TotalMinutes;
            var locked = rulesNow is null
                ? true
                : minutesStale > 15 ? cache.OfflineLocked(minutesStale) : rulesNow.Time.Locked;

            cache.SetLocked(locked);
            if (kidHere) enforcer.Enforce(locked, activity);
            await Task.Delay(TimeSpan.FromSeconds(cfg.PollSeconds), ct);
        }
    }
}
