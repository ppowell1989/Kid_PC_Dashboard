using System.Diagnostics;
using System.Runtime.InteropServices;

namespace KidsPc.Shell;

/// <summary>
/// Swallows the keys that open Windows' own panels over the kid's screen: the Windows key by itself
/// (Start) and with any other key (Win+A Quick Settings, Win+I Settings, Win+R Run, Win+E, Win+X, Win+N...),
/// and Ctrl+Esc. A low-level keyboard hook sees keys before Windows acts on them, in every program of
/// his session, for as long as this shell runs. Alt+Tab and everything games use is left alone.
/// </summary>
public static class KeyGuard
{
    private delegate IntPtr HookProc(int code, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, HookProc proc, IntPtr module, uint threadId);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int key);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string? name);

    private const int WH_KEYBOARD_LL = 13;
    private const int VK_ESCAPE = 0x1B, VK_CONTROL = 0x11, VK_LWIN = 0x5B, VK_RWIN = 0x5C;

    private static HookProc? _proc;     // kept so the delegate is not collected while Windows still calls it
    private static IntPtr _hook;

    /// <summary>Call once, on the UI thread (the hook is served by that thread's message loop).</summary>
    public static void Start()
    {
        if (_hook != IntPtr.Zero) return;
        try
        {
            _proc = Hook;
            using var module = Process.GetCurrentProcess().MainModule;
            _hook = SetWindowsHookEx(WH_KEYBOARD_LL, _proc, GetModuleHandle(module?.ModuleName), 0);
        }
        catch { /* the screen still works without it */ }
    }

    private static IntPtr Hook(int code, IntPtr wParam, IntPtr lParam)
    {
        if (code >= 0)
        {
            var key = Marshal.ReadInt32(lParam);    // KBDLLHOOKSTRUCT.vkCode
            if (key is VK_LWIN or VK_RWIN) return 1;
            if (key == VK_ESCAPE && (GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0) return 1;
        }
        return CallNextHookEx(_hook, code, wParam, lParam);
    }
}
