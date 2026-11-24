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
    private readonly DispatcherTimer _poll = new() { Interval = TimeSpan.FromSeconds(5) };
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
            DrawBackdrop(); await RefreshAsync(); _poll.Start(); StartChat();
            if (App.DemoScene is { } scene) await ShowDemoSceneAsync(scene);
        };
        SizeChanged += (_, _) => DrawBackdrop();
        Theme.Changed += OnThemeChanged;
        Launcher.Failed += (what, ex) => Dispatcher.Invoke(() => CouldNotStart(what, ex));
    }

    private readonly HashSet<string> _nudged = [];
    private DateTimeOffset _tidyUntil = DateTimeOffset.MinValue;

    /// <summary>While the PC is putting a game on by itself (and for a while after it started a store to do it), store
    /// windows that pop up are hidden so they can't cover his screen. See <see cref="StrayWindows"/>.</summary>
    private void TidyStoreWindows()
    {
        if (App.DemoScene is not null || _state is null) return;
        var signIn = _state.SignInNeeded ?? [];
        var busy = (_state.Installing?.Count ?? 0) > 0 || DateTimeOffset.Now < _tidyUntil;
        var hisOwn = DateTimeOffset.Now - Launcher.KidOpenedStoreAt < TimeSpan.FromMinutes(3);
        try
        {
            if (busy && !hisOwn) StrayWindows.Tidy(signIn);
            else StrayWindows.ShowAgain(signIn);
        }
        catch (Exception ex) { System.Diagnostics.Debug.WriteLine($"tidy failed: {ex.Message}"); }
    }

    private async Task RefreshAsync()
    {
        Clock.Text = DateTime.Now.ToString("dddd h:mm tt");
        try
        {
            _state = await _helper.GetAsync<ShellState>(new PipeRequest("state"));
            foreach (var n in _state?.Nudges ?? [])
                if (_nudged.Add(n.Id)) { Launcher.RunNudge(n); _tidyUntil = DateTimeOffset.Now.AddMinutes(10); }   // Steam or Battle.net, started as him, so a game a parent approved installs by itself
            TidyStoreWindows();
        }
        catch (Exception)
        {
            // Helper not answering: the helper is what enforces, so fail closed.
            ShowLock("The PC is starting up", "Give it a minute. If this stays, ask Mom or Dad.", canAsk: false);
            _ = RefreshSoonAsync();
            return;
        }
        Render();
    }

    private void Render()
    {
        if (_state?.Rules is not { } rules)
        {
            ShowLock("Waiting for the home server", "Ask Mom or Dad if this doesn't go away.", canAsk: false);
            _ = RefreshSoonAsync();
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

        if (_state.Locked && !t.Locked && _state.HubReachable)
        {
            // The helper has fresh rules that say "not locked" but hasn't applied them yet (it starts out locked
            // at sign-in). Don't tell him his time is up or offer to ask for more: it clears in a few seconds.
            ShowLock("Getting things ready", "One moment.", canAsk: false);
            _ = RefreshSoonAsync();
        }
        else if (_state.Locked)
        {
            var pending = rules.PendingRequests.Any(r => r.Kind == "time");
            var title = t.LockReason switch
            {
                "bedtime" => "It's bedtime", "paused" => "Mom or Dad paused the PC",
                "outside_hours" => "It's not play time right now", _ => "Time's up for today",
            };
            var sub = pending ? "Your request was sent. Waiting for Mom or Dad." : "You can ask for a little more time.";
            if (t.LockReason == "outside_hours" && !pending && NextPlayText(t.NextPlay) is { } next) sub = $"Play time starts {next}. {sub}";
            ShowLock(title, sub);
            _voice?.Close();
        }
        else
        {
            LockOverlay.Visibility = Visibility.Collapsed;
            Topmost = false;
        }

        BuildHomeTiles(rules);
        BuildFamily(rules);
        BuildCatalogue(rules);
    }

    /// <summary>"at 3:30 PM", "tomorrow at 3:30 PM" or "on Saturday at 9:00 AM", from the hub's next_play.</summary>
    private static string? NextPlayText(string? iso)
    {
        if (string.IsNullOrEmpty(iso) || !DateTimeOffset.TryParse(iso, out var at)) return null;
        var days = (at.Date - DateTime.Today).Days;   // the hub and the PC share the family's time zone
        var clock = at.ToString("h:mm tt");
        return days <= 0 ? $"at {clock}" : days == 1 ? $"tomorrow at {clock}" : $"on {at:dddd} at {clock}";
    }

    private bool _refreshingSoon;

    /// <summary>One extra look at the helper in 2 seconds, instead of waiting for the next regular poll.</summary>
    private async Task RefreshSoonAsync()
    {
        if (_refreshingSoon || App.DemoScene is not null) return;
        _refreshingSoon = true;
        try { await Task.Delay(2000); await RefreshAsync(); }
        finally { _refreshingSoon = false; }
    }

    /// <param name="canAsk">Show the "ask for more time" buttons. Only when time, bedtime or a pause is the reason.</param>
    private void ShowLock(string title, string sub, bool canAsk = true)
    {
        LockTitle.Text = title;
        LockSub.Text = sub;
        LockAsk.Visibility = canAsk ? Visibility.Visible : Visibility.Collapsed;
        if (!canAsk) LockStatus.Text = "";
        LockOverlay.Visibility = Visibility.Visible;
        Topmost = true;      // stay above games while locked
        Activate();
    }

    // ------------------------------------------------------------------ tiles

    private string _homeSig = "", _catalogueSig = "";

    private void BuildHomeTiles(Rules rules)
    {
        var installed = rules.Games.Where(g => _state!.InstalledGameIds.Contains(g.Id)).ToList();
        // Games a parent approved that are still being put on the PC show here too, with what is happening and a bar,
        // so he can see it coming (and so can a parent standing behind him).
        var coming = _state!.Installing ?? [];
        var percent = _state.InstallPercent ?? [];
        var onTheWay = rules.Games.Where(g => coming.ContainsKey(g.Id) && !_state.InstalledGameIds.Contains(g.Id)).ToList();
        // Only rebuild when something changed, so a tile doesn't lose its hover glow every time the shell polls.
        var sig = string.Join("|", installed.Select(g => $"{g.Id}:{g.Title}:{g.ImageUrl}")) + $"|friends:{rules.Friends.Count}|"
            + string.Join("|", onTheWay.Select(g => $"{g.Id}:{coming[g.Id]}:{percent.GetValueOrDefault(g.Id, -1)}"));
        if (sig == _homeSig && HomeTiles.Children.Count > 0) return;
        _homeSig = sig;
        HomeTiles.Children.Clear();
        foreach (var g in installed)
            HomeTiles.Children.Add(Tile($"{g.Title}\nPlay", () => Launcher.LaunchGame(g), image: g.ImageUrl));
        foreach (var g in onTheWay)
        {
            var words = coming[g.Id];
            var tile = Tile($"{g.Title}\n{words}", () => StatusLine.Text = ComingText(g, words), image: g.ImageUrl,
                percent: percent.TryGetValue(g.Id, out var p) ? p : null);
            tile.Opacity = 0.8;
            HomeTiles.Children.Add(tile);
        }
        HomeTiles.Children.Add(Tile("Web\nChrome", () => Launcher.LaunchChrome(), glyph: "\uE774"));
        HomeTiles.Children.Add(Tile("Chat with a friend\nVoice", OpenVoice, enabled: rules.Friends.Count > 0, glyph: "\uE720"));
        Recolour(HomeTiles);
    }

    private void BuildCatalogue(Rules rules)
    {
        var notInstalled = rules.Games.Where(g => !_state!.InstalledGameIds.Contains(g.Id)).ToList();
        // A game a parent approved installs by itself: its tile says how far along it is instead of "Install".
        var coming = _state!.Installing ?? [];
        string Caption(Game g) => coming.TryGetValue(g.Id, out var c) ? c : "Install";
        var sig = string.Join("|", notInstalled.Select(g => $"{g.Id}:{g.Title}:{g.ImageUrl}:{Caption(g)}"));
        if (sig == _catalogueSig && CatalogueTiles.Children.Count > 0) return;
        _catalogueSig = sig;
        CatalogueTiles.Children.Clear();
        foreach (var g in notInstalled)
            CatalogueTiles.Children.Add(Tile($"{g.Title}\n{Caption(g)}", async () =>
            {
                if (coming.TryGetValue(g.Id, out var words)) { StatusLine.Text = ComingText(g, words); return; }
                await InstallAsync(g);
            }, image: g.ImageUrl));
        if (notInstalled.Count == 0)
        {
            var none = new TextBlock { Text = "Everything approved is already installed.", FontSize = 18 };
            none.SetResourceReference(TextBlock.ForegroundProperty, "Muted");
            CatalogueTiles.Children.Add(none);
        }
        Recolour(CatalogueTiles);
    }

    /// <summary>What a tap on a game that's on its way tells him.</summary>
    private static string ComingText(Game g, string words) => words switch
    {
        "Waiting for Mom or Dad" => $"{g.Title} needs Mom or Dad before it can finish. They've been told.",
        _ when words.StartsWith("Downloading") => $"{g.Title} is downloading ({words[12..]}). It turns into Play when it's done.",
        _ => $"{g.Title} is on its way: {words.ToLowerInvariant()}. It turns into Play when it's ready.",
    };

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

    /// <summary>A tile: art (or an icon on a poster colour) on top, the name on a plate below.
    /// A second line in <paramref name="text"/> becomes the small coloured caption, e.g. "Portal 2\nInstall".</summary>
    private static Button Tile(string text, Action onClick, bool enabled = true, string? image = null, string? glyph = null, int? percent = null)
    {
        var art = LoadArt(image);
        var lines = text.Split('\n', 2);
        var plate = new StackPanel();
        plate.Children.Add(new TextBlock
        {
            Text = lines[0], FontSize = 20,
            TextTrimming = TextTrimming.CharacterEllipsis, TextWrapping = TextWrapping.Wrap, MaxHeight = 50, LineHeight = 24,
        });
        ((TextBlock)plate.Children[0]).SetResourceReference(TextBlock.FontFamilyProperty, "TileTitleFont");
        if (lines.Length > 1)
        {
            plate.Children.Add(new TextBlock
            {
                Text = lines[1].Replace("\n", "  \u2022  "), FontSize = 15, Margin = new Thickness(0, 1, 0, 0),
                TextTrimming = TextTrimming.CharacterEllipsis,
            });
            ((TextBlock)plate.Children[1]).SetResourceReference(TextBlock.ForegroundProperty, "Muted");
        }
        if (percent is { } pct)
        {
            var bar = new ProgressBar { Minimum = 0, Maximum = 100, Value = pct, Height = 6, Margin = new Thickness(0, 6, 0, 0), BorderThickness = new Thickness(0) };
            bar.SetResourceReference(ForegroundProperty, "Accent");
            bar.SetResourceReference(BackgroundProperty, "Line");
            plate.Children.Add(bar);
        }
        var b = new Button
        {
            Content = plate,
            IsEnabled = enabled,
        };
        b.SetResourceReference(StyleProperty, "Tile");
        if (art is not null) b.Background = new ImageBrush(art) { Stretch = Stretch.UniformToFill };
        else b.Tag = glyph ?? "\uE7FC";   // no art: a game controller
        b.Click += (_, _) => onClick();
        return b;
    }

    /// <summary>Dresses a row of tiles for the current theme: tiles with no art take the theme's poster colours in
    /// turn, and themes with coloured frames (TileFrames) give each tile the next frame colour.</summary>
    private void Recolour(Panel tiles)
    {
        var posters = TryFindResource("PosterColours") as Color[] ?? [Color.FromRgb(0x3D, 0x6B, 0xF5)];
        var frames = TryFindResource("TileFrames") as Color[];
        int i = 0, p = 0;
        foreach (var b in tiles.Children.OfType<Button>())
        {
            var caption = b.Content is StackPanel { Children.Count: > 1 } plate ? plate.Children[1] as TextBlock : null;
            if (frames is { Length: > 0 })
            {
                var frame = new SolidColorBrush(frames[i++ % frames.Length]);
                b.BorderBrush = frame;
                if (frames.Length > 1 && caption is not null) caption.Foreground = frame;
            }
            else
            {
                b.ClearValue(BorderBrushProperty);
                caption?.SetResourceReference(TextBlock.ForegroundProperty, "Muted");
            }
            if (b.Background is not ImageBrush)
            {
                var c = posters[p++ % posters.Length];
                var light = Color.FromRgb((byte)(c.R + (255 - c.R) / 4), (byte)(c.G + (255 - c.G) / 4), (byte)(c.B + (255 - c.B) / 4));
                b.Background = new LinearGradientBrush(light, c, 45);
            }
        }
    }

    /// <summary>A new look was picked: redraw what code draws (backdrop, tile colours, ticker). Everything
    /// declared in XAML follows the new theme by itself.</summary>
    private void OnThemeChanged()
    {
        DrawBackdrop();
        Recolour(HomeTiles); Recolour(CatalogueTiles); Recolour(SearchResults);
        RedrawFamily();   // family cards set a few colours in code: rebuild them in the new look
        if (_state?.Rules is { } rules) { SetTicker(rules.Kid.Name); ShowFuel(rules.Time); }
        if (ThemeOverlay.Visibility == Visibility.Visible) BuildThemeChoices();
    }

    /// <summary>The time left as a bar: green, then amber, then red.</summary>
    private void ShowFuel(TimeState t)
    {
        var total = Math.Max(1, t.Allowance + t.Granted);
        var left = Math.Clamp((double)t.MinutesLeft / total, 0, 1);
        FuelFill.Width = Math.Round(FuelLane.Width * left);
        FuelFill.SetResourceReference(Border.BackgroundProperty, t.Warn || left <= 0.12 ? "FuelEmpty" : left <= 0.34 ? "FuelLow" : "FuelOk");
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

    private void ShowHome(object s, RoutedEventArgs e) { HomeTiles.Visibility = Visibility.Visible; CataloguePanel.Visibility = Visibility.Collapsed; NavHome.Tag = "on"; NavMore.Tag = null; ShowFamilyPanel(); }
    private void ShowCatalogue(object s, RoutedEventArgs e) { HomeTiles.Visibility = Visibility.Collapsed; CataloguePanel.Visibility = Visibility.Visible; NavHome.Tag = null; NavMore.Tag = "on"; FamilyPanel.Visibility = Visibility.Collapsed; }

    private async Task InstallAsync(Game g)
    {
        try
        {
            var r = await _helper.GetAsync<InstallResult>(new PipeRequest("install", GameId: g.Id));
            switch (r?.Action)
            {
                case "uri": if (!Launcher.OpenUri(r.Uri!, g.Title)) return; break;      // it has already said why not
                case "exec": Process.Start(new ProcessStartInfo(r.Uri!, r.Args ?? "") { UseShellExecute = false }); break;
            }
            StatusLine.Text = r?.Message ?? "";
        }
        catch (HelperSaidNo no) { StatusLine.Text = no.Message; }
        catch (Exception ex) { CouldNotStart(g.Title, ex); }
    }

    /// <summary>A program for the kid is missing or would not start: he gets a plain sentence on his screen and
    /// his parents get a line in his activity feed. Windows' own error text never reaches him.</summary>
    private void CouldNotStart(string what, Exception ex)
    {
        StatusLine.Text = $"{what} couldn't start on this PC. A parent has been told.";
        _ = TellParentsAsync($"{what} could not start: {ex.Message}");
    }

    private async Task TellParentsAsync(string detail)
    {
        try { await _helper.GetAsync<object>(new PipeRequest("problem", Detail: detail)); }
        catch { /* the helper is restarting; the kid already has his answer */ }
    }

    /// <summary>What to show the kid when something failed: the helper's own sentence if it gave one, otherwise nothing technical.</summary>
    internal static string Plain(Exception ex) => ex is HelperSaidNo ? ex.Message : "Try again in a minute.";

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
        catch (Exception ex) { SearchStatus.Text = $"Search isn't working right now. {Plain(ex)}"; }
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
            AskStatus.Text = $"Couldn't send. {Plain(ex)}";
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
        catch (Exception ex) { target.Text = $"Couldn't send. {Plain(ex)}"; }
    }

    private void OpenVoice() => _ = OpenVoiceAsync(family: false);

    private string _voiceRoom = "";

    /// <summary>Opens the voice window: his own room (friends join with invite links) or the family room
    /// (both brothers, no link). Only one at a time, so switching rooms leaves the other one.</summary>
    private async Task OpenVoiceAsync(bool family)
    {
        var room = family ? "family" : "friends";
        if (_voice is { IsLoaded: true })
        {
            if (_voiceRoom == room) { _voice.Activate(); return; }
            _voice.Close();
        }
        try
        {
            var vt = await _helper.GetAsync<VoiceToken>(new PipeRequest(family ? "family_voice_token" : "voice_token"));
            var w = new VoiceWindow(vt!, _state!.Rules!.Kid.Name, Theme.Current, family) { Owner = this };
            w.Closed += async (_, _) =>
            {
                if (_voice != w) return;
                _voice = null; _voiceRoom = "";
                RedrawFamily();
                try { await _helper.GetAsync<object>(new PipeRequest("voice_state", Detail: "")); } catch { /* helper restarting */ }
            };
            _voice = w; _voiceRoom = room;
            w.Show();
            RedrawFamily();   // the Talk button turns into "You're in family voice" straight away
            try { await _helper.GetAsync<object>(new PipeRequest("voice_state", Detail: room)); } catch { /* only feeds the Family row */ }
        }
        catch (HelperSaidNo no) { StatusLine.Text = no.Message; }      // "Voice chat is off while the PC is locked."
        catch (Exception ex) { CouldNotStart("Voice chat", ex); }
    }

    private void ShutDown(object s, RoutedEventArgs e)
    {
        if (App.DemoScene is not null) return;
        Shutdown("/s /t 0");
    }
}
