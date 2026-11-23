using System.Runtime.InteropServices;

namespace KidsPc.Helper;

/// <summary>Who is signed in at the console. Enforcement only applies to the kid's own session,
/// so a parent signing in with their admin account is left alone.</summary>
public static class Sessions
{
    [DllImport("kernel32.dll")] private static extern uint WTSGetActiveConsoleSessionId();

    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WTSQuerySessionInformation(IntPtr server, uint sessionId, int infoClass, out IntPtr buffer, out int bytes);

    [DllImport("wtsapi32.dll")] private static extern void WTSFreeMemory(IntPtr memory);

    private const int WTSUserName = 5;

    public static uint ConsoleSessionId => WTSGetActiveConsoleSessionId();

    public static string? ConsoleUser()
    {
        var id = WTSGetActiveConsoleSessionId();
        if (id == 0xFFFFFFFF) return null;
        if (!WTSQuerySessionInformation(IntPtr.Zero, id, WTSUserName, out var buf, out _)) return null;
        try { return Marshal.PtrToStringUni(buf); }
        finally { WTSFreeMemory(buf); }
    }

    public static bool KidIsAtConsole(string kidUser) =>
        string.Equals(ConsoleUser(), kidUser, StringComparison.OrdinalIgnoreCase);
}
