using System.IO;
using System.Windows;
using KidsPc.Shared;
using Microsoft.Web.WebView2.Core;

namespace KidsPc.Shell;

/// <summary>
/// The kid's side of voice chat: a WebView2 page served from the app's own Assets folder under a
/// virtual https host, so the browser engine treats it as a secure origin and allows the microphone.
/// Topmost so it floats over a game.
/// </summary>
public partial class VoiceWindow : Window
{
    private const string Host = "kidspc.local";

    public VoiceWindow(VoiceToken vt, string kidName, string theme, bool family = false)
    {
        InitializeComponent();
        if (family) Title = VoiceTitle.Text = "Family voice";   // the brothers' room: no invite links, just family
        Loaded += async (_, _) =>
        {
          try
          {
            var userData = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "KidsPc", "WebView2");
            var env = await CoreWebView2Environment.CreateAsync(null, userData);
            await Web.EnsureCoreWebView2Async(env);
            var core = Web.CoreWebView2;
            core.SetVirtualHostNameToFolderMapping(Host, Path.Combine(AppContext.BaseDirectory, "Assets"), CoreWebView2HostResourceAccessKind.DenyCors);
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.PermissionRequested += (_, e) =>
            {
                var ours = e.Uri.StartsWith($"https://{Host}/", StringComparison.OrdinalIgnoreCase);
                e.State = ours && e.PermissionKind == CoreWebView2PermissionKind.Microphone
                    ? CoreWebView2PermissionState.Allow : CoreWebView2PermissionState.Deny;
            };
            // Keep the window on our page: no navigating off to the open web from here.
            core.NavigationStarting += (_, e) => { if (!e.Uri.StartsWith($"https://{Host}/")) e.Cancel = true; };
            core.NewWindowRequested += (_, e) => e.Handled = true;

            var hash = $"url={Uri.EscapeDataString(vt.Url)}&token={Uri.EscapeDataString(vt.Token)}&name={Uri.EscapeDataString(kidName)}&theme={Uri.EscapeDataString(theme)}" + (family ? "&room=family" : "");
            if (DemoOut is not null) core.NavigationCompleted += async (_, e) => await SaveDemoShotAsync(core, e);
            core.Navigate($"https://{Host}/voice.html#{hash}");
          }
          catch (Exception ex) when (DemoOut is not null) { File.WriteAllText(Path.Combine(DemoOut, "voice-page.txt"), ex.ToString()); }
        };
        Closed += (_, _) => Web.Dispose();   // leaving the window leaves the room
    }

    /// <summary>Demo runs only (CI screenshots): where to save a picture of the page exactly as WebView2 drew it.</summary>
    private static string? DemoOut => App.DemoScene is null ? null : Environment.GetEnvironmentVariable("KIDSPC_DEMO_OUT");

    private static async Task SaveDemoShotAsync(CoreWebView2 core, CoreWebView2NavigationCompletedEventArgs e)
    {
        try
        {
            await Task.Delay(1500);   // fonts in, first paint done
            using (var f = File.Create(Path.Combine(DemoOut!, "voice-page.png")))
                await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, f);
            File.WriteAllText(Path.Combine(DemoOut!, "voice-page.txt"), $"navigation success={e.IsSuccess} status={e.WebErrorStatus}");
        }
        catch (Exception ex) { File.WriteAllText(Path.Combine(DemoOut!, "voice-page.txt"), ex.ToString()); }
    }

    private void DragWindow(object s, System.Windows.Input.MouseButtonEventArgs e) => DragMove();

    private void Leave(object s, RoutedEventArgs e) => Close();
}
