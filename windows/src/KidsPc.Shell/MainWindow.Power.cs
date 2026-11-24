using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;

namespace KidsPc.Shell;

/// <summary>The Power button on the home screen. The kid has no Start menu, so this is the only way to
/// shut down, sleep, restart or sign out. All four run as the kid; Windows lets the person at the PC do them.</summary>
public partial class MainWindow
{
    private void OpenPower(object s, RoutedEventArgs e) => PowerOverlay.Visibility = Visibility.Visible;

    private void ClosePower(object s, RoutedEventArgs e) => PowerOverlay.Visibility = Visibility.Collapsed;

    private void DoPower(object s, RoutedEventArgs e)
    {
        PowerOverlay.Visibility = Visibility.Collapsed;
        if (App.DemoScene is not null) return;
        switch ((string)((Button)s).Tag)
        {
            case "shutdown": Shutdown("/s /t 0"); break;
            case "restart": Shutdown("/r /t 0"); break;
            case "signout": Shutdown("/l"); break;
            case "sleep":
                // false, false, false = sleep (not hibernate), let apps finish, keep wake timers.
                if (!SetSuspendState(false, false, false)) StatusLine.Text = "This PC can't go to sleep. Try Shut down.";
                break;
        }
    }

    private static void Shutdown(string args) =>
        Process.Start(new ProcessStartInfo("shutdown.exe", args) { CreateNoWindow = true, UseShellExecute = false });

    [DllImport("powrprof.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.U1)]
    private static extern bool SetSuspendState([MarshalAs(UnmanagedType.U1)] bool hibernate, [MarshalAs(UnmanagedType.U1)] bool forceCritical, [MarshalAs(UnmanagedType.U1)] bool disableWakeEvent);
}
