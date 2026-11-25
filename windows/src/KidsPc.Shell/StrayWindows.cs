using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace KidsPc.Shell;

/// <summary>
/// Keeps store windows that the PC opened by itself off the kid's screen. When a parent approves a game, the shell
/// starts Steam (or Battle.net) in the background so it downloads the game. With no Windows taskbar or tray, Steam
/// shows its windows anyway, and on Adam's test PC they came up as blank gray boxes over the home screen that he
/// couldn't close. This hides them (the store keeps running and keeps downloading).
///
/// Never hidden: a sign-in window (a grown-up has to see it to sign in once), any store window while the helper says
/// that store needs signing in, and anything the kid opened himself in the last few minutes.
/// </summary>
internal static class StrayWindows
{
    // Process name -> store. GUESS: these are the processes that own Steam's and Battle.net's windows.
    private static readonly Dictionary<string, string> Owners = new(StringComparer.OrdinalIgnoreCase)
    {
        ["steam"] = "steam", ["steamwebhelper"] = "steam", ["Battle.net"] = "battlenet",
    };

    // Words in a window title that mean a person has to type something there. GUESS at the titles.
    // "install" keeps Steam's own Install box up: the PC opens it on purpose for a free game that isn't on his account yet.
    private static readonly string[] SignInWords = ["sign in", "sign-in", "login", "log in", "steam guard", "verify", "install"];

    private static readonly Dictionary<IntPtr, string> Hidden = [];

    /// <summary>Hides visible store windows. Returns how many it hid this time.</summary>
    public static int Tidy(ICollection<string> leaveShowing)
    {
        ShowAgain(leaveShowing);
        var session = Process.GetCurrentProcess().SessionId;
        var storeOf = new Dictionary<uint, string?>();
        var hide = new List<(IntPtr, string)>();
        EnumWindows((h, _) =>
        {
            if (!IsWindowVisible(h)) return true;
            GetWindowThreadProcessId(h, out var pid);
            if (!storeOf.TryGetValue(pid, out var store)) storeOf[pid] = store = StoreOf(pid, session);
            if (store is null || leaveShowing.Contains(store)) return true;
            var title = Title(h);
            if (SignInWords.Any(w => title.Contains(w, StringComparison.OrdinalIgnoreCase))) return true;
            hide.Add((h, store));
            return true;
        }, IntPtr.Zero);
        foreach (var (h, store) in hide)
        {
            ShowWindow(h, SW_HIDE);
            Hidden[h] = store;
        }
        return hide.Count;
    }

    /// <summary>Puts back the windows of a store that now needs someone at the PC (to sign in).</summary>
    public static void ShowAgain(ICollection<string> stores)
    {
        if (stores.Count == 0 || Hidden.Count == 0) return;
        foreach (var (h, store) in Hidden.Where(kv => stores.Contains(kv.Value)).ToList())
        {
            Hidden.Remove(h);
            if (!IsWindow(h)) continue;
            ShowWindow(h, SW_SHOW);
            SetForegroundWindow(h);
        }
        foreach (var h in Hidden.Keys.Where(h => !IsWindow(h)).ToList()) Hidden.Remove(h);
    }

    private static string? StoreOf(uint pid, int session)
    {
        try
        {
            using var p = Process.GetProcessById((int)pid);
            return p.SessionId == session && Owners.TryGetValue(p.ProcessName, out var s) ? s : null;
        }
        catch { return null; }   // gone, or not ours to look at
    }

    private static string Title(IntPtr h)
    {
        var sb = new StringBuilder(256);
        GetWindowText(h, sb, sb.Capacity);
        return sb.ToString();
    }

    private const int SW_HIDE = 0, SW_SHOW = 5;
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
}
