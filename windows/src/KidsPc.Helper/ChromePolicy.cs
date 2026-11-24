using Microsoft.Win32;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>
/// Keeps Chrome's URL allow/block lists in step with the hub. The static lockdown policies
/// (incognito off, SafeSearch, extensions) are set once by setup\Set-ChromePolicy.ps1; this
/// class only rewrites the two lists that change when a parent approves a site or flips web_mode.
/// Chrome re-reads policy on its own every few minutes.
/// </summary>
public sealed class ChromePolicy(ILogger<ChromePolicy> log)
{
    private const string Root = @"SOFTWARE\Policies\Google\Chrome";
    private string _applied = "";

    public void Apply(WebRules web)
    {
        var fingerprint = web.Mode + "|" + string.Join(",", web.AllowedSites) + "|" + web.Dns?.Doh;
        if (fingerprint == _applied) return;

        using var chrome = Registry.LocalMachine.CreateSubKey(Root, writable: true);
        chrome.DeleteSubKeyTree("URLBlocklist", throwOnMissingSubKey: false);
        chrome.DeleteSubKeyTree("URLAllowlist", throwOnMissingSubKey: false);

        if (web.Mode == "allowlist")
        {
            WriteList(chrome, "URLBlocklist", ["*"]);
            // Chrome internals the extension and policy pages need, plus the approved sites.
            WriteList(chrome, "URLAllowlist", [.. web.AllowedSites, "chrome://*", "accounts.google.com"]);
        }
        // "filtered": no URL lists; the DNS filter (see NetworkFilter) + forced SafeSearch do the work.

        // Chrome's own secure DNS: pinned to the family filter so it can't route around the PC's DNS,
        // or off (when AdGuard on the LAN is the filter, Chrome must use the system resolver).
        if (!string.IsNullOrEmpty(web.Dns?.Doh))
        {
            chrome.SetValue("DnsOverHttpsMode", "secure", RegistryValueKind.String);
            chrome.SetValue("DnsOverHttpsTemplates", web.Dns.Doh, RegistryValueKind.String);
            chrome.SetValue("BuiltInDnsClientEnabled", 1, RegistryValueKind.DWord);
        }
        else
        {
            chrome.SetValue("DnsOverHttpsMode", "off", RegistryValueKind.String);
            chrome.DeleteValue("DnsOverHttpsTemplates", throwOnMissingValue: false);
            chrome.SetValue("BuiltInDnsClientEnabled", 0, RegistryValueKind.DWord);
        }

        _applied = fingerprint;
        log.LogInformation("chrome policy set: {Mode}, {Count} allowed sites", web.Mode, web.AllowedSites.Count);
    }

    private static void WriteList(RegistryKey parent, string name, IEnumerable<string> values)
    {
        using var key = parent.CreateSubKey(name, writable: true);
        var i = 1;
        foreach (var v in values) key.SetValue((i++).ToString(), v, RegistryValueKind.String);
    }
}
