using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>Starts the right launcher for a catalogue game. Runs as the kid (no elevation).</summary>
public static class Launcher
{
    // GUESS: default install locations. Non-secret, so they live here rather than in device.json.
    private const string BattleNet = @"C:\Program Files (x86)\Battle.net\Battle.net.exe";
    private const string Chrome = @"C:\Program Files\Google\Chrome\Application\chrome.exe";

    /// <summary>A game, Chrome or a launcher would not start: what it was, and why. The main window tells the
    /// kid in plain words and tells his parents; Windows' own error text is never shown to him.
    /// Without the catch a tile for a launcher that isn't installed crashed the kid's screen on every press.</summary>
    public static event Action<string, Exception>? Failed;

    /// <summary>False when it would not start (and <see cref="Failed"/> has said so).</summary>
    private static bool Try(string what, Action start)
    {
        try { start(); return true; }
        catch (Exception ex)   // not installed, moved, or Windows refused: none of these should take the screen down
        {
            Failed?.Invoke(what, ex);
            return false;
        }
    }

    /// <summary>When the kid last opened something that goes through Steam or Battle.net himself. Their windows are
    /// his to see for a while after that, so <see cref="StrayWindows"/> leaves them alone.</summary>
    public static DateTimeOffset KidOpenedStoreAt { get; private set; } = DateTimeOffset.MinValue;

    public static bool LaunchGame(Game g)
    {
        if (g.Launcher is "steam" or "battlenet") KidOpenedStoreAt = DateTimeOffset.Now;
        return Try(g.Title, () => StartGame(g));
    }

    private static void StartGame(Game g)
    {
        if (App.DemoScene is not null) return;
        switch (g.Launcher)
        {
            case "steam":
                Open($"steam://rungameid/{g.LaunchRef}");
                break;
            case "battlenet":
                Process.Start(new ProcessStartInfo(BattleNet, $"--exec=\"launch {g.LaunchRef}\"") { UseShellExecute = false });
                break;
            case "gamepass":
                // launch_ref is the app's AUMID, e.g. Microsoft.MinecraftUWP_8wekyb3d8bbwe!App
                AppActivation.Launch(g.LaunchRef);
                break;
            case "direct":
                Process.Start(new ProcessStartInfo(g.LaunchRef) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(g.LaunchRef) });
                break;
        }
    }

    public static bool LaunchChrome() => Try("Chrome", () =>
    {
        if (App.DemoScene is not null) return;
        Process.Start(new ProcessStartInfo(Chrome, "--start-maximized") { UseShellExecute = false });
    });

    /// <summary>A link from chat, in Chrome. Only web addresses; Chrome's own rules decide whether the site opens.</summary>
    public static bool OpenLink(string link) => Try("Chrome", () =>
    {
        if (App.DemoScene is not null) return;
        var url = link.StartsWith("www.", StringComparison.OrdinalIgnoreCase) ? "https://" + link : link;
        if (!Uri.TryCreate(url, UriKind.Absolute, out var u) || u.Scheme is not ("http" or "https")) return;
        var psi = new ProcessStartInfo(Chrome) { UseShellExecute = false };
        psi.ArgumentList.Add(u.AbsoluteUri);
        Process.Start(psi);
    });

    private const string Steam = @"C:\Program Files (x86)\Steam\steam.exe";

    /// <summary>Something the helper needs done as the kid, in his session, so a game a parent approved installs
    /// without anyone at the PC. Never shown to him; a failure only goes in the log via <see cref="Failed"/>'s caller.</summary>
    public static void RunNudge(Nudge n)
    {
        if (App.DemoScene is not null) return;
        try
        {
            switch (n.Kind)
            {
                case "steam_start" when File.Exists(Steam):
                    Process.Start(new ProcessStartInfo(Steam, "-silent") { UseShellExecute = false });
                    break;
                case "steam_restart" when File.Exists(Steam):
                    // Steam reads the games it has been given when it starts. Close it, wait, start it again in the tray.
                    _ = Task.Run(async () =>
                    {
                        Process.Start(new ProcessStartInfo(Steam, "-shutdown") { UseShellExecute = false })?.Dispose();
                        for (var i = 0; i < 30 && Process.GetProcessesByName("steam").Length > 0; i++) await Task.Delay(1000);
                        Process.Start(new ProcessStartInfo(Steam, "-silent") { UseShellExecute = false })?.Dispose();
                    });
                    break;
                case "battlenet_install" when File.Exists(BattleNet) && !string.IsNullOrWhiteSpace(n.Arg):
                    Process.Start(new ProcessStartInfo(BattleNet, $"--exec=\"install {n.Arg}\"") { UseShellExecute = false })?.Dispose();   // GUESS: Battle.net CLI verb
                    break;
            }
        }
        catch (Exception ex) { System.Diagnostics.Debug.WriteLine($"nudge {n.Kind} failed: {ex.Message}"); }
    }

    /// <summary>Opens a protocol URI. steam:// goes straight to steam.exe, because the protocol is often
    /// registered only for the account that installed Steam (the parent), not for the kid.</summary>
    public static bool OpenUri(string uri, string what)
    {
        KidOpenedStoreAt = DateTimeOffset.Now;
        return Try(what, () => Open(uri));
    }

    private static void Open(string uri)
    {
        if (App.DemoScene is not null) return;
        if (uri.StartsWith("steam://", StringComparison.OrdinalIgnoreCase) && File.Exists(Steam))
            Process.Start(new ProcessStartInfo(Steam, uri) { UseShellExecute = false });
        else
            Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true });
    }
}

/// <summary>Launches a packaged (Store / Game Pass) app without explorer.exe.</summary>
internal static class AppActivation
{
    [ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IApplicationActivationManager
    {
        int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, [MarshalAs(UnmanagedType.LPWStr)] string? arguments, int options, out uint processId);
        int ActivateForFile(string appUserModelId, IntPtr itemArray, string verb, out uint processId);
        int ActivateForProtocol(string appUserModelId, IntPtr itemArray, out uint processId);
    }

    [ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")]
    private class ApplicationActivationManager;

    public static void Launch(string aumid)
    {
        var mgr = (IApplicationActivationManager)new ApplicationActivationManager();
        Marshal.ThrowExceptionForHR(mgr.ActivateApplication(aumid, null, 0, out _));
    }
}
