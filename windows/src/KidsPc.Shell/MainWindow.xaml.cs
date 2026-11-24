using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using KidsPc.Shared;

namespace KidsPc.Shell;

public partial class MainWindow : Window
{
    private readonly HelperClient _helper = new();
    private readonly DispatcherTimer _poll = new() { Interval = TimeSpan.FromSeconds(15) };
    private ShellState? _state;
    private bool _warnedToday;
    private VoiceWindow? _voice;
    private readonly HashSet<int> _dismissed = [];
    private int _shownMessage = -1;

    public MainWindow()
    {
        InitializeComponent();
        _poll.Tick += async (_, _) => await RefreshAsync();
        Loaded += async (_, _) => { await RefreshAsync(); _poll.Start(); };
    }

    private async Task RefreshAsync()
    {
        Clock.Text = DateTime.Now.ToString("dddd h:mm tt");
        try
        {
            _state = await _helper.GetAsync<ShellState>(new PipeRequest("state"));
        }
        catch (Exception)
        {
            // Helper not answering: the helper is what enforces, so fail closed.
            ShowLock("The PC is starting up", "Give it a minute. If this stays, ask Mom or Dad.");
            return;
        }
        Render();
    }

    private void Render()
    {
        if (_state?.Rules is not { } rules)
        {
            ShowLock("Waiting for the home server", "Ask Mom or Dad if this doesn't go away.");
            return;
        }
        var t = rules.Time;
        Greeting.Text = $"Hi, {rules.Kid.Name}!";
        TimeLeft.Text = t.MinutesLeft >= 60 ? $"{t.MinutesLeft / 60}h {t.MinutesLeft % 60}m" : $"{t.MinutesLeft}m";
        StatusLine.Text = _state.HubReachable ? "" : "Can't reach the home server. Your time is still counting down.";

        if (t.Warn)
        {
            WarningText.Text = $"{t.MinutesLeft} minute{(t.MinutesLeft == 1 ? "" : "s")} left. Time to find a place to save.";
            WarningBanner.Visibility = Visibility.Visible;
            if (!_warnedToday) { _warnedToday = true; BringToFront(); }   // pull the shell in front of the game once so the warning is seen
        }
        if (!t.Warn) { WarningBanner.Visibility = Visibility.Collapsed; if (t.MinutesLeft > rules.WarningMinutes) _warnedToday = false; }

        var msg = _state.Messages?.LastOrDefault(m => !_dismissed.Contains(m.Id));
        if (msg is not null)
        {
            MessageText.Text = LockMessage.Text = $"{msg.From}: {msg.Text}";
            MessageBanner.Visibility = LockMessage.Visibility = Visibility.Visible;
            if (_shownMessage != msg.Id) { _shownMessage = msg.Id; BringToFront(); }   // bring the shell to the front once per message
        }
        else
        {
            MessageBanner.Visibility = LockMessage.Visibility = Visibility.Collapsed;
        }

        if (_state.Locked)
        {
            var pending = rules.PendingRequests.Any(r => r.Kind == "time");
            var title = t.LockReason switch { "bedtime" => "It's bedtime", "paused" => "Mom or Dad paused the PC", _ => "Time's up for today" };
            ShowLock(title, pending ? "Your request was sent. Waiting for Mom or Dad." : "You can ask for a little more time.");
            _voice?.Close();
        }
        else
        {
            LockOverlay.Visibility = Visibility.Collapsed;
            Topmost = false;
        }

        BuildHomeTiles(rules);
        BuildCatalogue(rules);
    }

    private void ShowLock(string title, string sub)
    {
        LockTitle.Text = title;
        LockSub.Text = sub;
        LockOverlay.Visibility = Visibility.Visible;
        Topmost = true;      // stay above games while locked
        Activate();
    }

    // ------------------------------------------------------------------ tiles

    private void BuildHomeTiles(Rules rules)
    {
        HomeTiles.Children.Clear();
        foreach (var g in rules.Games.Where(g => _state!.InstalledGameIds.Contains(g.Id)))
            HomeTiles.Children.Add(Tile(g.Title, () => Launcher.LaunchGame(g), image: g.ImageUrl));
        HomeTiles.Children.Add(Tile("Web (Chrome)", Launcher.LaunchChrome));
        HomeTiles.Children.Add(Tile("Chat with a friend", OpenVoice, enabled: rules.Friends.Count > 0));
    }

    private void BuildCatalogue(Rules rules)
    {
        CatalogueTiles.Children.Clear();
        var notInstalled = rules.Games.Where(g => !_state!.InstalledGameIds.Contains(g.Id)).ToList();
        foreach (var g in notInstalled)
            CatalogueTiles.Children.Add(Tile($"{g.Title}\nInstall", async () => await InstallAsync(g), image: g.ImageUrl));
        if (notInstalled.Count == 0)
            CatalogueTiles.Children.Add(new TextBlock { Text = "Everything approved is already installed.", FontSize = 16 });
    }

    private static readonly Dictionary<string, ImageSource?> Art = [];

    /// <summary>Game art (the Steam store header for Steam games), loaded once per URL and kept for the session.</summary>
    private static ImageSource? LoadArt(string? url)
    {
        if (string.IsNullOrWhiteSpace(url)) return null;
        if (Art.TryGetValue(url, out var cached)) return cached;
        try
        {
            var img = new BitmapImage();
            img.BeginInit();
            img.UriSource = new Uri(url);
            img.DecodePixelWidth = 600;
            img.CacheOption = BitmapCacheOption.OnLoad;
            img.DownloadFailed += (_, _) => Art[url] = null;
            img.EndInit();
            return Art[url] = img;
        }
        catch { return Art[url] = null; }
    }

    private static Button Tile(string text, Action onClick, bool enabled = true, string? image = null)
    {
        var art = LoadArt(image);
        UIElement label = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = art is null ? 20 : 16 };
        if (art is not null)
            label = new Border
            {
                Child = label, Padding = new Thickness(8, 3, 8, 3), CornerRadius = new CornerRadius(8),
                HorizontalAlignment = HorizontalAlignment.Left,
                Background = new SolidColorBrush(Color.FromArgb(0xC0, 0x10, 0x12, 0x16)),
            };
        var b = new Button
        {
            Style = (Style)Application.Current.Resources["Tile"],
            Content = label,
            IsEnabled = enabled,
            ToolTip = text,
        };
        if (art is not null) b.Background = new ImageBrush(art) { Stretch = Stretch.UniformToFill };
        b.Click += (_, _) => onClick();
        return b;
    }

    // ------------------------------------------------------------------ actions

    /// <summary>Windows won't let a background window steal focus with Activate() alone; a brief Topmost does it.</summary>
    private void BringToFront()
    {
        var wasTop = Topmost;
        Topmost = true;
        Activate();
        Topmost = wasTop;
    }

    private void DismissMessage(object s, RoutedEventArgs e)
    {
        if (_shownMessage >= 0) _dismissed.Add(_shownMessage);
        MessageBanner.Visibility = LockMessage.Visibility = Visibility.Collapsed;
    }

    private void ShowHome(object s, RoutedEventArgs e) { HomeTiles.Visibility = Visibility.Visible; CataloguePanel.Visibility = Visibility.Collapsed; }
    private void ShowCatalogue(object s, RoutedEventArgs e) { HomeTiles.Visibility = Visibility.Collapsed; CataloguePanel.Visibility = Visibility.Visible; }

    private async Task InstallAsync(Game g)
    {
        try
        {
            var r = await _helper.GetAsync<InstallResult>(new PipeRequest("install", GameId: g.Id));
            switch (r?.Action)
            {
                case "uri": Launcher.OpenUri(r.Uri!); break;
                case "exec": Process.Start(new ProcessStartInfo(r.Uri!, r.Args ?? "") { UseShellExecute = false }); break;
            }
            StatusLine.Text = r?.Message ?? "";
        }
        catch (Exception ex) { StatusLine.Text = ex.Message; }
    }

    private async void AskForGame(object s, RoutedEventArgs e)
    {
        var title = AskGameBox.Text.Trim();
        if (title.Length == 0) return;
        await SendRequest("game", title, $"Sent! Mom or Dad will look at \"{title}\".");
        AskGameBox.Clear();
    }

    private async void AskForTime(object s, RoutedEventArgs e) =>
        await SendRequest("time", (string)((Button)s).Tag, "Sent! Waiting for Mom or Dad.", LockStatus);

    private async void AskForTimeHome(object s, RoutedEventArgs e) =>
        await SendRequest("time", "15", "Asked for 15 more minutes. Waiting for Mom or Dad.");

    private async Task SendRequest(string kind, string detail, string okText, TextBlock? target = null)
    {
        target ??= StatusLine;
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("request", Kind: kind, Detail: detail));
            target.Text = okText;
        }
        catch (Exception ex) { target.Text = $"Couldn't send: {ex.Message}"; }
    }

    private async void OpenVoice()
    {
        if (_voice is { IsLoaded: true }) { _voice.Activate(); return; }
        try
        {
            var vt = await _helper.GetAsync<VoiceToken>(new PipeRequest("voice_token"));
            _voice = new VoiceWindow(vt!, _state!.Rules!.Kid.Name) { Owner = this };
            _voice.Show();
        }
        catch (Exception ex) { StatusLine.Text = ex.Message; }
    }

    private void ShutDown(object s, RoutedEventArgs e) =>
        Process.Start(new ProcessStartInfo("shutdown.exe", "/s /t 0") { CreateNoWindow = true, UseShellExecute = false });
}
