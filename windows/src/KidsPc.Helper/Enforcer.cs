using System.Diagnostics;

namespace KidsPc.Helper;

/// <summary>
/// The hard edge of the time limit. The shell shows the lock screen; this makes sure games
/// actually stop, even if a game is running full-screen over the shell.
/// </summary>
public sealed class Enforcer(EventQueue events, ILogger<Enforcer> log)
{
    private readonly HashSet<int> _logged = [];

    public void Enforce(bool locked, Activity activity)
    {
        foreach (var p in activity.Processes)
        {
            var reason = locked ? "time is up"
                : p.Kind is "steam" or "battlenet" && p.Approved is null ? "game not approved"
                : p.Kind == "browser" ? "only Chrome is allowed"
                : null;
            if (reason is null) continue;
            Kill(p, reason);
        }
    }

    private void Kill(GameProcess p, string reason)
    {
        try
        {
            using var proc = Process.GetProcessById(p.Pid);
            proc.Kill(entireProcessTree: true);
            if (_logged.Add(p.Pid))
            {
                log.LogInformation("stopped {Path} ({Reason})", p.Path, reason);
                var what = p.Approved?.Title ?? (p.Kind == "chrome" ? "Chrome" : Path.GetFileNameWithoutExtension(p.Path));
                events.Add(reason switch { "time is up" => "stopped_time_up", "only Chrome is allowed" => "browser_blocked", _ => "game_blocked" }, what);
            }
        }
        catch (ArgumentException) { /* already exited */ }
        catch (Exception ex) { log.LogWarning("could not stop {Pid}: {Msg}", p.Pid, ex.Message); }
    }
}
