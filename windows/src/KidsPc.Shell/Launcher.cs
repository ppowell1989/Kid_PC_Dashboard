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

    public static void LaunchGame(Game g)
    {
        switch (g.Launcher)
        {
            case "steam":
                OpenUri($"steam://rungameid/{g.LaunchRef}");
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

    public static void LaunchChrome() =>
        Process.Start(new ProcessStartInfo(Chrome, "--start-maximized") { UseShellExecute = false });

    private const string Steam = @"C:\Program Files (x86)\Steam\steam.exe";

    /// <summary>Opens a protocol URI. steam:// goes straight to steam.exe, because the protocol is often
    /// registered only for the account that installed Steam (the parent), not for the kid.</summary>
    public static void OpenUri(string uri)
    {
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
