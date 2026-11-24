using System.Diagnostics;
using System.Windows;

namespace KidsPc.Shell;

public partial class App : Application
{
    private static int _restarts;

    /// <summary>Set by "--demo &lt;scene&gt;": canned data instead of the helper, for looking at the design (see Demo.cs).</summary>
    public static string? DemoScene { get; private set; }

    protected override void OnStartup(StartupEventArgs e)
    {
        // --restart N counts crash restarts so a crash at startup cannot loop forever.
        var i = Array.IndexOf(e.Args, "--restart");
        if (i >= 0 && i + 1 < e.Args.Length) int.TryParse(e.Args[i + 1], out _restarts);
        var d = Array.IndexOf(e.Args, "--demo");
        if (d >= 0) DemoScene = d + 1 < e.Args.Length ? e.Args[d + 1] : "home";
        var th = Array.IndexOf(e.Args, "--theme");   // demo runs can ask for a look; otherwise the kid's saved choice
        Theme.Load(th >= 0 && th + 1 < e.Args.Length ? e.Args[th + 1] : null);
        // This program IS the Windows shell for the kid's account (see setup\Set-KidShell.ps1).
        // If it ever crashes, start a fresh copy rather than leave a black screen.
        DispatcherUnhandledException += (_, args) =>
        {
            args.Handled = true;
            Restart();
        };
        AppDomain.CurrentDomain.UnhandledException += (_, _) => Restart();
        base.OnStartup(e);
    }

    private static void Restart()
    {
        if (DemoScene is not null) Environment.Exit(2);   // a demo that crashed should say so, not turn into the real shell
        // Only crashes soon after starting count towards giving up. A screen that ran for a while and then
        // tripped starts the count again; otherwise five mishaps in one long day left him with a black screen.
        var n = _restarts + 1;
        try { if (DateTime.Now - Process.GetCurrentProcess().StartTime > TimeSpan.FromMinutes(1)) n = 1; } catch { /* keep counting */ }
        if (n <= 5)
        {
            try { Process.Start(Environment.ProcessPath!, $"--restart {n}"); } catch { /* nothing more we can do */ }
        }
        Environment.Exit(1);
    }
}
