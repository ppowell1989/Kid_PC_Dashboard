using System.Diagnostics;
using System.Net;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>
/// Points every active network adapter at the family DNS filter from the hub (Cloudflare for
/// Families by default, AdGuard on the LAN once it runs). The kid isn't an admin, so he can't
/// change it back. Re-applied every 10 minutes so a new adapter (Wi-Fi, USB tether) is covered too.
/// </summary>
public sealed class NetworkFilter(EventQueue events, ILogger<NetworkFilter> log)
{
    private string _applied = "";
    private DateTimeOffset _at;

    public AppStatus Status { get; private set; } = new("installing", "waiting for the hub");

    public void Apply(DnsRules? dns)
    {
        dns ??= new("none", [], "");
        var servers = dns.Servers.Where(s => IPAddress.TryParse(s, out _)).ToList();   // only literal IPs reach the command line
        var fingerprint = string.Join(",", servers);
        if (fingerprint == _applied && DateTimeOffset.Now - _at < TimeSpan.FromMinutes(10)) return;

        var set = servers.Count == 0
            ? "Set-DnsClientServerAddress -InterfaceIndex $i -ResetServerAddresses -ErrorAction Stop"
            : $"Set-DnsClientServerAddress -InterfaceIndex $i -ServerAddresses @({string.Join(",", servers.Select(s => $"'{s}'"))}) -ErrorAction Stop";
        // Only adapters that are up AND have DNS client settings (some virtual adapters have none).
        // Fails only if no adapter could be set.
        var script = "$up = @(Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object ifIndex); "
                   + "$ids = @(Get-DnsClientServerAddress -ErrorAction SilentlyContinue | Where-Object { $up -contains $_.InterfaceIndex } | ForEach-Object InterfaceIndex | Sort-Object -Unique); "
                   + "$ok = 0; foreach ($i in $ids) { try { " + set + "; $ok++ } catch { Write-Warning $_ } }; "
                   + "Clear-DnsClientCache; if ($ok -eq 0) { Write-Error 'no network adapter accepted the DNS setting'; exit 1 }";
        try
        {
            using var p = Process.Start(new ProcessStartInfo("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script])
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true, RedirectStandardOutput = true })!;
            var err = p.StandardError.ReadToEnd();
            p.WaitForExit(30_000);
            if (p.ExitCode != 0) throw new InvalidOperationException(err.Trim());
            if (fingerprint != _applied)
            {
                log.LogInformation("DNS filter {Filter}: {Servers}", dns.Filter, fingerprint == "" ? "PC default" : fingerprint);
                events.Add("web_filter", dns.Filter);
            }
            _applied = fingerprint;
            _at = DateTimeOffset.Now;
            Status = dns.Filter switch
            {
                "cloudflare_family" => new("installed", "Cloudflare for Families DNS"),
                "adguard" => new("installed", $"AdGuard at {fingerprint}"),
                _ => new("failed", "no web filter (dns_filter: none)"),
            };
        }
        catch (Exception ex)
        {
            log.LogWarning("could not set DNS filter: {Msg}", ex.Message);
            Status = new("failed", "could not set DNS: " + ex.Message);
        }
    }
}
