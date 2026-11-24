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
    private readonly IHelper _helper = App.DemoScene is null ? new PipeHelper() : new DemoHelper(App.DemoScene);
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
        Loaded += async (_, _) =>
        {
            DrawSpace(); await RefreshAsync(); _poll.Start(); StartChat();
            if (App.DemoScene is { } scene) await ShowDemoSceneAsync(scene);
        };
        SizeChanged += (_, _) => DrawSpace();
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
        ShowFuel(t);
        SetTicker(rules.Kid.Name);
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

    private string _homeSig = "", _catalogueSig = "";

    private void BuildHomeTiles(Rules rules)
    {
        var installed = rules.Games.Where(g => _state!.InstalledGameIds.Contains(g.Id)).ToList();
        // Only rebuild when something changed, so a tile doesn't lose its hover glow every time the shell polls.
        var sig = string.Join("|", installed.Select(g => $"{g.Id}:{g.Title}:{g.ImageUrl}")) + $"|friends:{rules.Friends.Count}";
        if (sig == _homeSig && HomeTiles.Children.Count > 0) return;
        _homeSig = sig;
        HomeTiles.Children.Clear();
        foreach (var g in installed)
            HomeTiles.Children.Add(Tile($"{g.Title}\nPlay", () => Launcher.LaunchGame(g), image: g.ImageUrl));
        HomeTiles.Children.Add(Tile("Web\nChrome", Launcher.LaunchChrome, glyph: "\uE774"));
        HomeTiles.Children.Add(Tile("Chat with a friend\nVoice", OpenVoice, enabled: rules.Friends.Count > 0, glyph: "\uE720"));
        Recolour(HomeTiles);
    }

    private void BuildCatalogue(Rules rules)
    {
        var notInstalled = rules.Games.Where(g => !_state!.InstalledGameIds.Contains(g.Id)).ToList();
        var sig = string.Join("|", notInstalled.Select(g => $"{g.Id}:{g.Title}:{g.ImageUrl}"));
        if (sig == _catalogueSig && CatalogueTiles.Children.Count > 0) return;
        _catalogueSig = sig;
        CatalogueTiles.Children.Clear();
        foreach (var g in notInstalled)
            CatalogueTiles.Children.Add(Tile($"{g.Title}\nInstall", async () => await InstallAsync(g), image: g.ImageUrl));
        if (notInstalled.Count == 0)
            CatalogueTiles.Children.Add(new TextBlock { Text = "Everything approved is already installed.", FontSize = 18, Foreground = (Brush)FindResource("Muted") });
        Recolour(CatalogueTiles);
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

    /// <summary>The neon frame colours tiles take in turn.</summary>
    private static readonly Color[] TileColours =
    [
        Color.FromRgb(0x22, 0xE4, 0xFF), Color.FromRgb(0xFF, 0x2E, 0x97), Color.FromRgb(0xFF, 0xD2, 0x3F),
        Color.FromRgb(0x7D, 0xFF, 0x5A), Color.FromRgb(0xFF, 0x8A, 0x00), Color.FromRgb(0x9B, 0x5C, 0xFF),
    ];

    /// <summary>A tile: art (or an icon on a poster colour) on top, the name on a plate below.
    /// A second line in <paramref name="text"/> becomes the small coloured caption, e.g. "Portal 2\nInstall".</summary>
    private static Button Tile(string text, Action onClick, bool enabled = true, string? image = null, string? glyph = null)
    {
        var art = LoadArt(image);
        var lines = text.Split('\n', 2);
        var plate = new StackPanel();
        plate.Children.Add(new TextBlock
        {
            Text = lines[0], FontSize = 20, FontWeight = FontWeights.SemiBold, Foreground = Brushes.White,
            TextTrimming = TextTrimming.CharacterEllipsis,
        });
        if (lines.Length > 1)
            plate.Children.Add(new TextBlock
            {
                Text = lines[1].Replace("\n", "  \u2022  "), FontSize = 13, Margin = new Thickness(0, 1, 0, 0),
                FontFamily = (FontFamily)Application.Current.Resources["Display"], TextTrimming = TextTrimming.CharacterEllipsis,
            });
        var b = new Button
        {
            Style = (Style)Application.Current.Resources["Tile"],
            Content = plate,
            IsEnabled = enabled,
            ToolTip = lines[0],
        };
        if (art is not null) b.Background = new ImageBrush(art) { Stretch = Stretch.UniformToFill };
        else b.Tag = glyph ?? "\uE7FC";   // no art: a game controller
        b.Click += (_, _) => onClick();
        return b;
    }

    /// <summary>Gives each tile in a row the next frame colour, and matches its caption and poster to it.</summary>
    private static void Recolour(Panel tiles)
    {
        var i = 0;
        foreach (var b in tiles.Children.OfType<Button>())
        {
            var c = TileColours[i++ % TileColours.Length];
            var brush = new SolidColorBrush(c);
            b.BorderBrush = brush;
            if (b.Content is StackPanel { Children.Count: > 1 } plate && plate.Children[1] is TextBlock caption) caption.Foreground = brush;
            if (b.Background is not ImageBrush)
                b.Background = new RadialGradientBrush(Color.FromArgb(0xFF, c.R, c.G, c.B), Color.FromRgb((byte)(c.R / 5), (byte)(c.G / 6), (byte)(c.B / 3 + 20)))
                    { GradientOrigin = new Point(0.3, 0.25), Center = new Point(0.4, 0.4), RadiusX = 0.9, RadiusY = 0.9 };
        }
    }

    /// <summary>The time left as a fuel gauge: green, then amber, then red.</summary>
    private void ShowFuel(TimeState t)
    {
        var total = Math.Max(1, t.Allowance + t.Granted);
        var left = Math.Clamp((double)t.MinutesLeft / total, 0, 1);
        FuelFill.Width = Math.Round(348 * left);
        FuelFill.Background = (Brush)FindResource(t.Warn || left <= 0.12 ? "FuelEmpty" : left <= 0.34 ? "FuelLow" : "FuelOk");
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

    private void ShowHome(object s, RoutedEventArgs e) { HomeTiles.Visibility = Visibility.Visible; CataloguePanel.Visibility = Visibility.Collapsed; NavHome.Tag = "on"; NavMore.Tag = null; }
    private void ShowCatalogue(object s, RoutedEventArgs e) { HomeTiles.Visibility = Visibility.Collapsed; CataloguePanel.Visibility = Visibility.Visible; NavHome.Tag = null; NavMore.Tag = "on"; }

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
        await SendRequest("game", title, $"Sent! Mom or Dad will look at \"{title}\".", SearchStatus);
        AskGameBox.Clear();
    }

    private async void AskForTime(object s, RoutedEventArgs e) =>
        await SendRequest("time", (string)((Button)s).Tag, "Sent! Waiting for Mom or Dad.", LockStatus);

    private async void AskForTimeHome(object s, RoutedEventArgs e) =>
        await SendRequest("time", "15", "Asked for 15 more minutes. Waiting for Mom or Dad.");

    private async void SearchKeyDown(object s, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == System.Windows.Input.Key.Enter) { e.Handled = true; await SearchGamesAsync(); }
    }

    private async void SearchGames(object s, RoutedEventArgs e) => await SearchGamesAsync();

    private async Task SearchGamesAsync()
    {
        var q = AskGameBox.Text.Trim();
        if (q.Length < 2) return;
        SearchStatus.Text = "Searching…";
        SearchResults.Children.Clear();
        try
        {
            var hits = await _helper.GetAsync<List<SteamGame>>(new PipeRequest("game_search", Detail: q)) ?? [];
            SearchStatus.Text = hits.Count == 0 ? "Nothing found. You can still ask for it by name." : "Tap a game to ask Mom or Dad for it.";
            foreach (var g in hits)
            {
                var asked = AlreadyAsked(g.Name);
                var label = (string.IsNullOrEmpty(g.Esrb) ? g.Name : $"{g.Name}\nRated {g.Esrb}") + (asked ? "\nAsked, waiting" : "");
                SearchResults.Children.Add(Tile(label, () => ShowAsk(g), enabled: !asked, image: g.ImageUrl));
            }
            Recolour(SearchResults);
        }
        catch (Exception ex) { SearchStatus.Text = $"Search isn't working right now: {ex.Message}"; }
    }

    private bool AlreadyAsked(string title) =>
        _state?.Rules?.PendingRequests.Any(r => r.Kind == "game" && r.Detail.Equals(title, StringComparison.OrdinalIgnoreCase)) == true
        || _askedThisSession.Contains(title);

    private readonly HashSet<string> _askedThisSession = new(StringComparer.OrdinalIgnoreCase);
    private SteamGame? _asking;

    private void ShowAsk(SteamGame g)
    {
        _asking = g;
        AskTitle.Text = g.Name;
        AskRating.Text = string.IsNullOrEmpty(g.Esrb) ? "No rating listed" : $"Rated {g.Esrb}";
        AskBlurb.Text = System.Net.WebUtility.HtmlDecode(g.Description ?? "");
        AskArt.ImageSource = LoadArt(g.ImageUrl);
        AskNote.Text = AskStatus.Text = "";
        AskSend.IsEnabled = true;
        AskOverlay.Visibility = Visibility.Visible;
        AskNote.Focus();
    }

    private void CancelAsk(object s, RoutedEventArgs e) { AskOverlay.Visibility = Visibility.Collapsed; _asking = null; }

    private async void ConfirmAsk(object s, RoutedEventArgs e)
    {
        if (_asking is not { } g) return;
        AskSend.IsEnabled = false;
        AskStatus.Text = "Sending…";
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("request", Kind: "game", Detail: g.Name, Note: AskNote.Text.Trim(), Ref: $"steam:{g.Appid}"));
            _askedThisSession.Add(g.Name);
            AskOverlay.Visibility = Visibility.Collapsed;
            await SearchGamesAsync();   // redraw so the tile shows "Asked, waiting"
            SearchStatus.Text = $"Sent! Mom or Dad will look at \"{g.Name}\". If they say yes, it shows up in the approved games above.";
        }
        catch (Exception ex)
        {
            AskStatus.Text = $"Couldn't send: {ex.Message}";
            AskSend.IsEnabled = true;
        }
    }

    private async Task SendRequest(string kind, string detail, string okText, TextBlock? target = null, string? reference = null)
    {
        target ??= StatusLine;
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("request", Kind: kind, Detail: detail, Ref: reference));
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

    private void ShutDown(object s, RoutedEventArgs e)
    {
        if (App.DemoScene is not null) return;
        Process.Start(new ProcessStartInfo("shutdown.exe", "/s /t 0") { CreateNoWindow = true, UseShellExecute = false });
    }
}
