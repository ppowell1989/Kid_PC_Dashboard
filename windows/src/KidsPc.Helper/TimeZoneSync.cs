using System.Diagnostics;
using Microsoft.Win32;

namespace KidsPc.Helper;

/// <summary>
/// Keeps the PC's time zone on the family's, as the hub names it. The hub decides when "today" starts and when
/// bedtime is, so a PC left on Windows' default (Pacific) showed 8:07 while the house was at 11:07 and the lock
/// came at a time the clock on his screen didn't explain. Windows' automatic time zone is turned off once we set
/// it, so location guesses (or a VM's host) can't move it back. Needs the helper's admin rights.
/// </summary>
public sealed class TimeZoneSync(EventQueue events, ILogger<TimeZoneSync> log)
{
    private string _tried = "";

    public void Apply(string? iana)
    {
        if (string.IsNullOrWhiteSpace(iana)) return;
        if (!TimeZoneInfo.TryConvertIanaIdToWindowsId(iana, out var windowsId))
        {
            if (_tried != iana) log.LogWarning("hub time zone {Tz} has no Windows equivalent", iana);
            _tried = iana;
            return;
        }
        TimeZoneInfo.ClearCachedData();
        if (string.Equals(TimeZoneInfo.Local.Id, windowsId, StringComparison.OrdinalIgnoreCase)) { _tried = ""; return; }
        if (_tried == iana) return;   // one attempt per hub value; a failure is logged once, not every minute
        _tried = iana;
        try
        {
            var was = TimeZoneInfo.Local.Id;
            using (var svc = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\tzautoupdate", writable: true))
                svc?.SetValue("Start", 4, RegistryValueKind.DWord);   // 4 = disabled: "Set time zone automatically" off
            using var p = Process.Start(new ProcessStartInfo("tzutil.exe", $"/s \"{windowsId}\"") { UseShellExecute = false, CreateNoWindow = true })!;
            p.WaitForExit(10_000);
            TimeZoneInfo.ClearCachedData();
            if (p.ExitCode != 0) throw new InvalidOperationException($"tzutil exited {p.ExitCode}");
            log.LogInformation("time zone set to {Tz} (was {Was})", windowsId, was);
            events.Add("timezone_set", $"{windowsId} (was {was})");
        }
        catch (Exception ex)
        {
            log.LogWarning("could not set the time zone to {Tz}: {Msg}", windowsId, ex.Message);
        }
    }
}
