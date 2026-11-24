using System.Diagnostics;
using System.Net.Http.Json;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>Carries out one-off commands from the dashboard and reports the result back.</summary>
public sealed class CommandRunner(HttpClient http, StateCache cache, AppInstaller apps, ILogger<CommandRunner> log)
{
    private readonly Dictionary<int, (bool Ok, string Result)> _done = [];   // ran, but the hub may not know yet

    public async Task RunAsync(List<HubCommand> commands, CancellationToken ct)
    {
        foreach (var c in commands)
        {
            if (!_done.TryGetValue(c.Id, out var outcome))
            {
                outcome = Execute(c);
                _done[c.Id] = outcome;
                log.LogInformation("command {Id} {Kind}: {Result}", c.Id, c.Kind, outcome.Result);
            }
            var r = await http.PostAsJsonAsync($"/api/device/commands/{c.Id}", new { ok = outcome.Ok, result = outcome.Result }, ct);
            if (r.IsSuccessStatusCode) _done.Remove(c.Id);
        }
    }

    private (bool, string) Execute(HubCommand c)
    {
        try
        {
            switch (c.Kind)
            {
                case "message":
                    cache.AddMessage(new ParentMessage(c.Id, c.CreatedBy, c.Arg, DateTimeOffset.Now));
                    return (true, "shown");
                case "restart":
                case "shutdown":
                    var flag = c.Kind == "restart" ? "/r" : "/s";
                    Process.Start(new ProcessStartInfo("shutdown.exe", $"{flag} /t 60 /c \"{c.CreatedBy} is {(c.Kind == "restart" ? "restarting" : "shutting down")} this PC in 1 minute.\"")
                        { UseShellExecute = false, CreateNoWindow = true });
                    cache.AddMessage(new ParentMessage(c.Id, c.CreatedBy, $"This PC will {(c.Kind == "restart" ? "restart" : "shut down")} in 1 minute. Save your game!", DateTimeOffset.Now));
                    return (true, "in 60 seconds");
                case "install_app":
                    apps.InstallNow(c.Arg);
                    return (true, "install started");
                default:
                    return (false, $"unknown command {c.Kind}");
            }
        }
        catch (Exception ex) { return (false, ex.Message); }
    }
}
