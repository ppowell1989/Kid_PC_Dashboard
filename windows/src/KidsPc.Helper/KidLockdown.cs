using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Principal;
using KidsPc.Shared;
using Microsoft.Win32;

namespace KidsPc.Helper;

/// <summary>
/// Keeps the kid's account the way setup left it, every few seconds while he is signed in:
///  - the Windows settings that keep system panels off his screen (they live in his own part of the
///    registry, which his account may write, so they are put back if they ever change; this is also how
///    a PC set up by an older build gets settings a newer build adds);
///  - his sign-in still starts the kid screen and not something else;
///  - the kid screen is running. If it was closed, it is started again; if that fails he is signed out,
///    because a session without it is a session nothing is watching.
/// A parent's own sign-in is never touched.
/// </summary>
public sealed class KidLockdown(DeviceConfig cfg, EventQueue events, ILogger<KidLockdown> log)
{
    private static readonly string ShellExe = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "Shell", "KidsPcShell.exe"));

    /// <summary>Ticks in a row without the kid screen before it is started again. Long enough for a sign-in
    /// that is still on its way, and for the screen's own restart after a crash.</summary>
    private const int MissingTicksNeeded = 3;

    private static readonly (string Key, string Name, int Value)[] Policies =
    [
        (@"Software\Microsoft\Windows\CurrentVersion\Policies\System", "DisableTaskMgr", 1),
        (@"Software\Microsoft\Windows\CurrentVersion\Policies\System", "DisableRegistryTools", 1),
        (@"Software\Microsoft\Windows\CurrentVersion\Policies\System", "DisableChangePassword", 1),
        (@"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "NoRun", 1),
        (@"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "NoControlPanel", 1),
        (@"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "NoWinKeys", 1),               // Win+ shortcuts
        (@"Software\Policies\Microsoft\Windows\System", "DisableCMD", 1),
        (@"Software\Policies\Microsoft\Windows\Explorer", "DisableControlCenter", 1),                    // Quick Settings (Win+A)
        (@"Software\Policies\Microsoft\Windows\Explorer", "DisableNotificationCenter", 1),               // notifications and calendar (Win+N)
    ];

    private string? _sid;
    private int _missing;
    private DateTimeOffset _lastStart;
    private bool _checkedOnce;

    public void Tick(bool kidHere, uint session, bool updating)
    {
        if (!kidHere || !File.Exists(ShellExe)) { _missing = 0; return; }
        try { KeepSettings(); }
        catch (Exception ex) { log.LogWarning("could not check the kid's Windows settings: {Msg}", ex.Message); }
        try { KeepShell(session, updating); }
        catch (Exception ex) { log.LogWarning("could not check the kid screen: {Msg}", ex.Message); }
    }

    private void KeepSettings()
    {
        _sid ??= ((SecurityIdentifier)new NTAccount(cfg.KidUser).Translate(typeof(SecurityIdentifier))).Value;
        using var hive = Registry.Users.OpenSubKey(_sid, writable: true);
        if (hive is null) return;       // his registry is only there while he is signed in
        var first = !_checkedOnce;
        _checkedOnce = true;
        var changed = new List<string>();
        foreach (var (key, name, value) in Policies)
        {
            using var k = hive.CreateSubKey(key, writable: true);
            if (k.GetValue(name) is int now && now == value) continue;
            k.SetValue(name, value, RegistryValueKind.DWord);
            changed.Add(name);
        }
        using (var winlogon = hive.CreateSubKey(@"Software\Microsoft\Windows NT\CurrentVersion\Winlogon", writable: true))
        {
            if (!string.Equals(winlogon.GetValue("Shell") as string, ShellExe, StringComparison.OrdinalIgnoreCase))
            {
                winlogon.SetValue("Shell", ShellExe, RegistryValueKind.String);
                changed.Add("Shell");
            }
        }
        if (changed.Count == 0) return;
        log.LogInformation("put back the kid's Windows settings: {Names}", string.Join(", ", changed));
        // The first pass after an update adds whatever is new in this build; that is not worth a line on the
        // dashboard. Anything that changes after that was changed on the PC.
        if (!first) events.Add("lockdown_restored", string.Join(", ", changed));
    }

    private void KeepShell(uint session, bool updating)
    {
        var running = false;
        foreach (var p in Process.GetProcessesByName("KidsPcShell"))
        {
            using (p) { if ((uint)p.SessionId == session) running = true; }
        }
        if (running || updating) { _missing = 0; return; }
        if (++_missing < MissingTicksNeeded) return;
        _missing = 0;
        // Twice in two minutes means it will not stay up (or he found a way to keep closing it).
        var again = DateTimeOffset.Now - _lastStart < TimeSpan.FromMinutes(2);
        _lastStart = DateTimeOffset.Now;
        if (!again)
        {
            try
            {
                StartInSession(session, ShellExe);
                log.LogWarning("the kid screen was not running; started it again");
                events.Add("shell_restarted", "");
                return;
            }
            catch (Exception ex) { log.LogWarning("could not start the kid screen: {Msg}", ex.Message); }
        }
        log.LogWarning("the kid screen will not stay up; signing the kid out");
        events.Add("shell_restarted", "signed out");
        Sessions.SignOut(session);
    }

    // ---- start a program as the signed-in user, from this service

    [DllImport("wtsapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool WTSQueryUserToken(uint sessionId, out IntPtr token);

    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateEnvironmentBlock(out IntPtr environment, IntPtr token, [MarshalAs(UnmanagedType.Bool)] bool inherit);

    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyEnvironmentBlock(IntPtr environment);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcessAsUser(IntPtr token, string? application, System.Text.StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, [MarshalAs(UnmanagedType.Bool)] bool inheritHandles, uint flags,
        IntPtr environment, string? currentDirectory, ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInformation);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string? lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;

    private static void StartInSession(uint session, string exe)
    {
        if (!WTSQueryUserToken(session, out var token)) throw new Win32Exception(Marshal.GetLastWin32Error());
        var env = IntPtr.Zero;
        try
        {
            if (!CreateEnvironmentBlock(out env, token, false)) env = IntPtr.Zero;
            var si = new STARTUPINFO { cb = Marshal.SizeOf<STARTUPINFO>(), lpDesktop = @"winsta0\default" };
            var cmd = new System.Text.StringBuilder($"\"{exe}\"");
            if (!CreateProcessAsUser(token, exe, cmd, IntPtr.Zero, IntPtr.Zero, false,
                    env == IntPtr.Zero ? 0 : CREATE_UNICODE_ENVIRONMENT, env, Path.GetDirectoryName(exe), ref si, out var pi))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
        }
        finally
        {
            if (env != IntPtr.Zero) DestroyEnvironmentBlock(env);
            CloseHandle(token);
        }
    }
}
