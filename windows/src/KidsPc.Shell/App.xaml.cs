using System.Diagnostics;
using System.Windows;

namespace KidsPc.Shell;

public partial class App : Application
{
    private static int _restarts;

    protected override void OnStartup(StartupEventArgs e)
    {
        // --restart N counts crash restarts so a crash at startup cannot loop forever.
        var i = Array.IndexOf(e.Args, "--restart");
        if (i >= 0 && i + 1 < e.Args.Length) int.TryParse(e.Args[i + 1], out _restarts);
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
        if (_restarts < 5)
        {
            try { Process.Start(Environment.ProcessPath!, $"--restart {_restarts + 1}"); } catch { /* nothing more we can do */ }
        }
        Environment.Exit(1);
    }
}
