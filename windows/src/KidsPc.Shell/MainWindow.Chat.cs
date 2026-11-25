using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>
/// Chat docked in the bottom-right corner. One pill per conversation: Family (Mom, Dad and both kids), his
/// brothers, and each friend a parent invited. The hub decides which ones he has and when they work (only
/// Family while the PC is locked); Mom and Dad can read all of them, and the panel says so.
/// Pictures come from his newest screenshots or the clipboard; there is no file browser to wander off in.
/// </summary>
public partial class MainWindow
{
    private readonly DispatcherTimer _chatPoll = new() { Interval = TimeSpan.FromSeconds(4) };
    private readonly Dictionary<string, long> _chatSeen = [];        // per conversation: newest message from someone else he has seen
    private readonly Dictionary<string, long> _chatTold = [];        // per conversation: newest message a card was shown for
    private readonly Dictionary<long, BitmapSource> _pictures = [];  // chat pictures already fetched
    private List<ChatConv> _convs = [new("family", "Family", "family", 0, 0, true)];
    private ChatList? _chatList;
    private string _conv = "family";
    private long _chatLast;
    private bool _convsStarted, _chatBusy, _convsBusy;
    private int _chatTick;

    private bool ChatOpen => ChatPanel.Visibility == Visibility.Visible;
    private ChatConv CurrentConv => _convs.FirstOrDefault(c => c.Id == _conv) ?? _convs[0];

    private void StartChat()
    {
        // Messages every 4 s while open; the list of conversations (for the unread badge and the new-message card) every 8 s.
        _chatPoll.Tick += async (_, _) =>
        {
            if (++_chatTick % 2 == 0 || ChatOpen) await PollConvsAsync();
            if (ChatOpen) await PollChatAsync();
        };
        _chatPoll.Start();
        BuildConvPills();
        _ = PollConvsAsync();
    }

    private async Task PollConvsAsync()
    {
        if (_convsBusy) return;
        _convsBusy = true;
        try
        {
            var list = await _helper.GetAsync<ChatList>(new PipeRequest("chat_convs"));
            if (list is null || list.Convs.Count == 0) return;
            _chatList = list;
            _convs = list.Convs;
            foreach (var c in _convs)
                if (!_convsStarted || !_chatSeen.ContainsKey(c.Id)) _chatSeen[c.Id] = c.LastOtherId;   // what was there before isn't "new"
            // Someone else wrote in a chat he isn't looking at (it is closed, on another conversation, or a game is
            // covering it): show a card over whatever is on screen and make a sound. Once per message.
            ChatConv? news = null;
            foreach (var c in _convs)
            {
                var told = _chatTold.GetValueOrDefault(c.Id, _convsStarted ? _chatSeen.GetValueOrDefault(c.Id) : c.LastOtherId);
                if (c.LastOtherId > told && !(ChatOpen && c.Id == _conv && IsActive)) news = c;
                _chatTold[c.Id] = Math.Max(told, c.LastOtherId);
            }
            if (news is not null)
            {
                var to = news.Id;
                ChatToast.Show(this, "New chat message", news.Kind == "family" ? "Family chat" : news.Title, async () =>
                {
                    BringToFront();
                    if (!ChatOpen) ToggleChat(this, new RoutedEventArgs());
                    else if (_conv != to) await SwitchConvAsync(to);
                });
            }
            _convsStarted = true;
            if (ChatOpen) _chatSeen[_conv] = CurrentConv.LastOtherId;
            if (_convs.All(c => c.Id != _conv)) await SwitchConvAsync("family");   // a parent switched that chat off
            BuildConvPills();
            UpdateBadge();
            UpdateComposer();
        }
        catch { /* the message poll says when the home server can't be reached */ }
        finally { _convsBusy = false; }
    }

    private async Task PollChatAsync()
    {
        if (_chatBusy) return;
        _chatBusy = true;
        var conv = _conv;
        try
        {
            var msgs = await _helper.GetAsync<List<ChatMessage>>(new PipeRequest("chat", Detail: _chatLast.ToString(), Ref: conv)) ?? [];
            if (conv != _conv) return;   // he switched while this was on its way
            ChatStatus.Visibility = Visibility.Collapsed;
            if (msgs.Count == 0) return;
            var atBottom = ChatScroll.VerticalOffset >= ChatScroll.ScrollableHeight - 30;
            foreach (var m in msgs) ChatLog.Children.Add(Bubble(m));
            while (ChatLog.Children.Count > 200) ChatLog.Children.RemoveAt(0);
            _chatLast = msgs[^1].Id;
            var other = msgs.Where(m => !m.Mine).Select(m => m.Id).DefaultIfEmpty(0).Max();
            if (ChatOpen && other > _chatSeen.GetValueOrDefault(conv)) _chatSeen[conv] = other;
            if (atBottom || ChatOpen) ChatScroll.ScrollToEnd();
            BuildConvPills();
            UpdateBadge();
        }
        catch (HelperSaidNo no) { ShowChatStatus(no.Message); }   // "This chat is off while the PC is locked."
        catch { ShowChatStatus("Chat can't reach the home server right now."); }
        finally { _chatBusy = false; }
    }

    private void ShowChatStatus(string text)
    {
        ChatStatus.Text = text;
        ChatStatus.Visibility = Visibility.Visible;
    }

    private bool Unread(ChatConv c) => c.LastOtherId > _chatSeen.GetValueOrDefault(c.Id) && !(ChatOpen && c.Id == _conv);

    private void UpdateBadge()
    {
        var n = _convs.Count(Unread);
        ChatBadge.Visibility = n > 0 ? Visibility.Visible : Visibility.Collapsed;
        ChatBadgeText.Text = n > 9 ? "9+" : n.ToString();
    }

    private string _pillSig = "";
    private void BuildConvPills()
    {
        var sig = string.Join("|", _convs.Select(c => $"{c.Id}:{c.Title}:{Unread(c)}:{c.Id == _conv}"));
        if (sig == _pillSig) return;
        _pillSig = sig;
        ChatConvs.Children.Clear();
        ChatConvs.Visibility = _convs.Count > 1 ? Visibility.Visible : Visibility.Collapsed;
        foreach (var c in _convs)
        {
            var on = c.Id == _conv;
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            row.Children.Add(new TextBlock { Text = c.Title, VerticalAlignment = VerticalAlignment.Center });
            if (Unread(c))
            {
                var dot = new Border { Width = 9, Height = 9, CornerRadius = new CornerRadius(5), Margin = new Thickness(7, 1, 0, 0), VerticalAlignment = VerticalAlignment.Center };
                dot.SetResourceReference(Border.BackgroundProperty, "Bad");
                row.Children.Add(dot);
            }
            var b = new Button { Content = row, Margin = new Thickness(0, 0, 8, 6), FontSize = 15, Padding = new Thickness(14, 6, 14, 7), Tag = c.Id };
            b.SetResourceReference(StyleProperty, "Pill");
            if (!on)
            {
                b.SetResourceReference(BackgroundProperty, "Well");
                b.SetResourceReference(ForegroundProperty, "Text");
            }
            b.Click += async (_, _) => await SwitchConvAsync((string)b.Tag);
            ChatConvs.Children.Add(b);
        }
    }

    private async Task SwitchConvAsync(string conv)
    {
        if (conv == _conv && ChatLog.Children.Count > 0) return;
        _conv = conv;
        _chatLast = 0;
        ChatLog.Children.Clear();
        ChatStatus.Visibility = Visibility.Collapsed;
        ClosePicturePicker(this, new RoutedEventArgs());
        _chatSeen[conv] = CurrentConv.LastOtherId;
        BuildConvPills();
        UpdateBadge();
        UpdateComposer();
        await PollChatAsync();
    }

    /// <summary>Title, who can read it, and whether he can write or send a picture here right now.</summary>
    private void UpdateComposer()
    {
        var c = CurrentConv;
        ChatTitle.Text = c.Kind switch { "family" => "Family chat", "kids" => $"Chat with {c.Title}", _ => $"Chat with {c.Title}" };
        ChatWho.Text = c.Kind switch
        {
            "family" => "Your whole family is in this chat, Mom and Dad too.",
            _ => "Mom and Dad can read this chat, and see every picture, video and link in it.",
        };
        var can = c.CanSend;
        ChatBox.IsEnabled = ChatSendButton.IsEnabled = can;
        ChatPictureButton.Visibility = _chatList is { Pictures: false } ? Visibility.Collapsed : Visibility.Visible;
        ChatPictureButton.IsEnabled = can;
        if (!can && c.Note.Length > 0) ShowChatStatus($"{c.Note}. Family chat still works.");
    }

    private UIElement Bubble(ChatMessage m)
    {
        // Colours are tied to the theme by name, so old bubbles follow when the kid picks another look.
        var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 8), HorizontalAlignment = m.Mine ? HorizontalAlignment.Right : HorizontalAlignment.Left, MaxWidth = 320 };
        TextBlock Small(string text, Thickness margin)
        {
            var t = new TextBlock { Text = text, FontSize = 12, Margin = margin, HorizontalAlignment = stack.HorizontalAlignment };
            t.SetResourceReference(TextBlock.ForegroundProperty, "Muted");
            return t;
        }
        if (!m.Mine) stack.Children.Add(Small(m.Sender, new Thickness(4, 0, 0, 2)));
        var content = new StackPanel();
        if (m.Picture) content.Children.Add(PictureIn(m));
        if (m.Text.Length > 0)
        {
            var words = Words(m);
            if (m.Picture) words.Margin = new Thickness(0, 6, 0, 0);
            content.Children.Add(words);
        }
        var bubble = new Border { CornerRadius = new CornerRadius(14), Padding = m.Picture ? new Thickness(6, 6, 6, 7) : new Thickness(12, 7, 12, 8),
                                  HorizontalAlignment = stack.HorizontalAlignment, Child = content };
        // me / Mom or Dad / brother or friend
        bubble.SetResourceReference(Border.BackgroundProperty, m.Removed ? "Well" : m.Mine ? "Accent" : m.KidId is null && !m.Friend ? "BubbleParent" : "BubbleSibling");
        stack.Children.Add(bubble);
        if (DateTimeOffset.TryParse(m.At, out var at)) stack.Children.Add(Small(at.ToString("h:mm tt"), new Thickness(4, 2, 4, 0)));
        return stack;
    }

    /// <summary>The words, with each link a link he can click. Links open in Chrome, under the same rules as any site.</summary>
    private TextBlock Words(ChatMessage m)
    {
        var words = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 16, FontStyle = m.Removed ? FontStyles.Italic : FontStyles.Normal };
        words.SetResourceReference(TextBlock.ForegroundProperty, m.Removed ? "Muted" : m.Mine ? "OnAccent" : "BubbleText");
        var text = m.Text;
        var at = 0;
        foreach (var link in m.Links ?? [])
        {
            var i = text.IndexOf(link, at, StringComparison.Ordinal);
            if (i < 0) continue;
            if (i > at) words.Inlines.Add(new Run(text[at..i]));
            var h = new Hyperlink(new Run(link)) { Cursor = Cursors.Hand };
            h.SetResourceReference(TextElement.ForegroundProperty, m.Mine ? "OnAccent" : "Accent");
            h.Click += (_, _) => Launcher.OpenLink(link);
            words.Inlines.Add(h);
            at = i + link.Length;
        }
        if (at < text.Length) words.Inlines.Add(new Run(text[at..]));
        return words;
    }

    private FrameworkElement PictureIn(ChatMessage m)
    {
        var img = new Image { MaxWidth = 300, MaxHeight = 220, Stretch = Stretch.Uniform, Cursor = Cursors.Hand, HorizontalAlignment = HorizontalAlignment.Left };
        var frame = new Border { CornerRadius = new CornerRadius(10), ClipToBounds = true, MinWidth = 120, MinHeight = 80, Child = img };
        frame.SetResourceReference(Border.BackgroundProperty, "Well");
        _ = LoadPictureAsync(m.Id, img);
        if (!m.Video)
        {
            img.MouseLeftButtonUp += (_, _) => { if (img.Source is { } src) ShowBig(src, null); };
            return frame;
        }
        // A clip: its first frame with a play button over it.
        var play = new Border { Width = 54, Height = 54, CornerRadius = new CornerRadius(27), Background = new SolidColorBrush(Color.FromArgb(0xB0, 0, 0, 0)),
                                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, IsHitTestVisible = false,
                                Child = new TextBlock { Text = "\uE768", FontSize = 24, Foreground = Brushes.White, HorizontalAlignment = HorizontalAlignment.Center,
                                                        VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(4, 0, 0, 0) } };
        ((TextBlock)play.Child).SetResourceReference(TextBlock.FontFamilyProperty, "Icons");
        frame.Child = null;
        var holder = new Grid { Cursor = Cursors.Hand, MinWidth = 160, MinHeight = 100 };
        holder.Children.Add(img);
        holder.Children.Add(play);
        frame.Child = holder;
        holder.MouseLeftButtonUp += async (_, _) => await PlayVideoAsync(m.Id);
        return frame;
    }

    private async Task LoadPictureAsync(long id, Image img)
    {
        if (!_pictures.TryGetValue(id, out var bmp))
        {
            try
            {
                var p = await _helper.GetAsync<ChatPicture>(new PipeRequest("chat_picture_get", Detail: id.ToString()));
                if (p is null) return;
                bmp = Decode(Convert.FromBase64String(p.Data));
                _pictures[id] = bmp;
            }
            catch { return; }   // the bubble stays an empty frame; the words still show
        }
        img.Source = bmp;
    }

    private static BitmapImage Decode(byte[] bytes, int width = 0)
    {
        var b = new BitmapImage();
        b.BeginInit();
        b.CacheOption = BitmapCacheOption.OnLoad;
        if (width > 0) b.DecodePixelWidth = width;
        b.StreamSource = new MemoryStream(bytes);
        b.EndInit();
        b.Freeze();
        return b;
    }

    private void ShowBig(ImageSource? picture, Uri? video)
    {
        PictureBigImage.Source = picture;
        PictureBigImage.Visibility = video is null ? Visibility.Visible : Visibility.Collapsed;
        VideoBig.Visibility = video is null ? Visibility.Collapsed : Visibility.Visible;
        if (video is not null) { VideoBig.Source = video; VideoBig.Position = TimeSpan.Zero; VideoBig.Play(); }
        PictureBig.Visibility = Visibility.Visible;
    }

    private void ClosePictureBig(object s, MouseButtonEventArgs e)
    {
        VideoBig.Stop();
        VideoBig.Source = null;
        PictureBig.Visibility = Visibility.Collapsed;
    }

    private void VideoEnded(object s, RoutedEventArgs e) { VideoBig.Position = TimeSpan.Zero; VideoBig.Pause(); }

    /// <summary>Clips are fetched once into his temp folder and played from there.</summary>
    private async Task PlayVideoAsync(long id)
    {
        var file = Path.Combine(Path.GetTempPath(), "kidspc-chat", $"{id}.mp4");
        if (!File.Exists(file))
        {
            ShowChatStatus("Getting the video…");
            try
            {
                var v = await _helper.GetAsync<ChatPicture>(new PipeRequest("chat_video_get", Detail: id.ToString()));
                if (v is null) return;
                Directory.CreateDirectory(Path.GetDirectoryName(file)!);
                await File.WriteAllBytesAsync(file, Convert.FromBase64String(v.Data));
            }
            catch (Exception ex) { ShowChatStatus($"The video won't play. {Plain(ex)}"); return; }
            ChatStatus.Visibility = Visibility.Collapsed;
        }
        ShowBig(null, new Uri(file));
    }

    private async void ToggleChat(object s, RoutedEventArgs e)
    {
        var open = !ChatOpen;
        ChatPanel.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        ChatPill.Visibility = open ? Visibility.Collapsed : Visibility.Visible;
        if (!open) return;
        // Open on the conversation with something new in it, else where he was.
        var fresh = _convs.FirstOrDefault(Unread);
        if (fresh is not null && fresh.Id != _conv) { await SwitchConvAsync(fresh.Id); }
        else
        {
            _chatSeen[_conv] = CurrentConv.LastOtherId;
            BuildConvPills();
            UpdateBadge();
            UpdateComposer();
            ChatScroll.ScrollToEnd();
            await PollChatAsync();
        }
        if (ChatBox.IsEnabled) ChatBox.Focus();
    }

    private async void ChatKeyDown(object s, KeyEventArgs e)
    {
        if (e.Key == Key.Enter) { e.Handled = true; await SendChatAsync(); }
    }

    /// <summary>Ctrl+V with a picture on the clipboard sends the picture (with whatever he has typed).</summary>
    private async void ChatPreviewKeyDown(object s, KeyEventArgs e)
    {
        if (e.Key == Key.V && Keyboard.Modifiers.HasFlag(ModifierKeys.Control) && SafeClipboardImage() is { } img && ChatPictureButton.Visibility == Visibility.Visible)
        {
            e.Handled = true;
            await SendPictureAsync(img);
        }
    }

    private async void SendChat(object s, RoutedEventArgs e) => await SendChatAsync();

    private async Task SendChatAsync()
    {
        var text = ChatBox.Text.Trim();
        if (text.Length == 0) return;
        ChatBox.Text = "";
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("chat_send", Detail: text, Ref: _conv));
        }
        catch (Exception ex)
        {
            ChatBox.Text = text;
            ShowChatStatus($"Didn't send. {Plain(ex)}");
            return;
        }
        await PollChatAsync();
    }

    // ------------------------------------------------------------------ pictures

    private static BitmapSource? SafeClipboardImage()
    {
        try { return Clipboard.ContainsImage() ? Clipboard.GetImage() : null; }
        catch { return null; }   // another program holding the clipboard
    }

    /// <summary>Where Windows, the Xbox Game Bar and Steam put screenshots for this kid.</summary>
    private static IEnumerable<string> ScreenshotFolders()
    {
        var pics = Environment.GetFolderPath(Environment.SpecialFolder.MyPictures);
        var vids = Environment.GetFolderPath(Environment.SpecialFolder.MyVideos);
        yield return Path.Combine(pics, "Screenshots");
        yield return pics;
        yield return Path.Combine(vids, "Captures");
        var steam = @"C:\Program Files (x86)\Steam\userdata";
        if (Directory.Exists(steam))
            foreach (var d in SafeDirs(steam, "*").SelectMany(u => SafeDirs(Path.Combine(u, "760", "remote"), "*")))
                yield return Path.Combine(d, "screenshots");
    }

    private static IEnumerable<string> SafeDirs(string path, string pattern)
    {
        try { return Directory.Exists(path) ? Directory.GetDirectories(path, pattern) : []; }
        catch { return []; }
    }

    private static readonly string[] PictureTypes = [".png", ".jpg", ".jpeg", ".gif", ".bmp"];
    private static readonly string[] VideoTypes = [".mp4", ".mov", ".wmv"];
    private const long MaxVideoBytes = 100L * 1024 * 1024;   // the hub's limit; it keeps a much smaller copy

    /// <summary>His newest clips: the Xbox Game Bar's captures (Windows key + Alt + R is blocked, but games and Steam can save there too).</summary>
    private static List<FileInfo> RecentVideos(int n = 8)
    {
        var dirs = new[] { Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyVideos), "Captures"),
                           Environment.GetFolderPath(Environment.SpecialFolder.MyVideos) };
        var files = new List<FileInfo>();
        foreach (var dir in dirs.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            try { if (Directory.Exists(dir)) files.AddRange(new DirectoryInfo(dir).EnumerateFiles().Where(f => VideoTypes.Contains(f.Extension.ToLowerInvariant()))); }
            catch { /* a folder he can't read */ }
        }
        return files.OrderByDescending(f => f.LastWriteTimeUtc).Take(n).ToList();
    }

    private Button VideoTile(FileInfo f)
    {
        var label = new StackPanel { VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center };
        var glyph = new TextBlock { Text = "\uE714", FontSize = 26, HorizontalAlignment = HorizontalAlignment.Center };
        glyph.SetResourceReference(TextBlock.FontFamilyProperty, "Icons");
        label.Children.Add(glyph);
        label.Children.Add(new TextBlock { Text = f.LastWriteTime.ToString("MMM d, h:mm tt"), FontSize = 12, Margin = new Thickness(0, 6, 0, 0), HorizontalAlignment = HorizontalAlignment.Center });
        var b = new Button { Width = 130, Height = 96, Margin = new Thickness(0, 0, 10, 10), Padding = new Thickness(0), Cursor = Cursors.Hand, ToolTip = f.Name,
                             Content = label, BorderThickness = new Thickness(0) };
        b.SetResourceReference(BackgroundProperty, "Well");
        b.SetResourceReference(ForegroundProperty, "Text");
        var path = f.FullName;
        b.Click += async (_, _) => await SendVideoAsync(path);
        return b;
    }

    private async Task SendVideoAsync(string path)
    {
        byte[] bytes;
        try
        {
            if (new FileInfo(path).Length > MaxVideoBytes) { PickerNote.Text = "That video is too big. Pick a shorter one (up to 30 seconds)."; return; }
            bytes = await File.ReadAllBytesAsync(path);
        }
        catch { PickerNote.Text = "That video can't be opened. Pick another one."; return; }
        var caption = ChatBox.Text.Trim();
        ClosePicturePicker(this, new RoutedEventArgs());
        ShowChatStatus("Sending the video… this can take a minute.");
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("chat_video", Detail: Convert.ToBase64String(bytes), Note: caption, Ref: _conv));
            ChatBox.Text = "";
            ChatStatus.Visibility = Visibility.Collapsed;
        }
        catch (Exception ex) { ShowChatStatus($"Didn't send. {Plain(ex)}"); return; }
        await PollChatAsync();
    }

    private static List<FileInfo> RecentPictures(int n = 24)
    {
        var files = new List<FileInfo>();
        foreach (var dir in ScreenshotFolders().Distinct(StringComparer.OrdinalIgnoreCase))
        {
            try
            {
                if (!Directory.Exists(dir)) continue;
                files.AddRange(new DirectoryInfo(dir).EnumerateFiles().Where(f => PictureTypes.Contains(f.Extension.ToLowerInvariant())));
            }
            catch { /* a folder he can't read */ }
        }
        return files.GroupBy(f => f.FullName, StringComparer.OrdinalIgnoreCase).Select(g => g.First())
                    .OrderByDescending(f => f.LastWriteTimeUtc).Take(n).ToList();
    }

    private void OpenPicturePicker(object s, RoutedEventArgs e)
    {
        PickerGrid.Children.Clear();
        PasteButton.Visibility = SafeClipboardImage() is null ? Visibility.Collapsed : Visibility.Visible;
        List<FileInfo> recent = App.DemoScene is null ? RecentPictures() : [];
        foreach (var f in recent)
        {
            BitmapImage thumb;
            try { thumb = Decode(File.ReadAllBytes(f.FullName), 200); }
            catch { continue; }
            var b = new Button { Width = 130, Height = 96, Margin = new Thickness(0, 0, 10, 10), Padding = new Thickness(0), Cursor = Cursors.Hand, ToolTip = f.Name,
                                 Content = new Image { Source = thumb, Stretch = Stretch.UniformToFill }, BorderThickness = new Thickness(0) };
            b.SetResourceReference(BackgroundProperty, "Well");
            var path = f.FullName;
            b.Click += async (_, _) =>
            {
                BitmapSource full;
                try { full = Decode(File.ReadAllBytes(path)); }
                catch { PickerNote.Text = "That picture can't be opened. Pick another one."; return; }
                await SendPictureAsync(full);
            };
            PickerGrid.Children.Add(b);
        }
        List<FileInfo> clips = App.DemoScene is null && _chatList is { Videos: true } ? RecentVideos() : [];
        foreach (var f in clips) PickerGrid.Children.Add(VideoTile(f));
        PickerNote.Text = recent.Count == 0 && clips.Count == 0 && PasteButton.Visibility != Visibility.Visible
            ? "No screenshots yet. Take one in a game (F12 in Steam), or press Print Screen and come back to paste it."
            : clips.Count > 0 ? "Your newest screenshots and clips (up to 30 seconds). Mom and Dad can see everything you send."
            : "Your newest screenshots. Mom and Dad can see every picture you send.";
        PicturePicker.Visibility = Visibility.Visible;
    }

    private void ClosePicturePicker(object s, RoutedEventArgs e) => PicturePicker.Visibility = Visibility.Collapsed;

    private async void PastePicture(object s, RoutedEventArgs e)
    {
        if (SafeClipboardImage() is { } img) await SendPictureAsync(img);
    }

    private async Task SendPictureAsync(BitmapSource img)
    {
        string data;
        try { data = Convert.ToBase64String(Shrink(img)); }
        catch { ShowChatStatus("That picture can't be sent. Try another one."); return; }
        var caption = ChatBox.Text.Trim();
        ClosePicturePicker(this, new RoutedEventArgs());
        ShowChatStatus("Sending the picture…");
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("chat_picture", Detail: data, Note: caption, Ref: _conv));
            ChatBox.Text = "";
            ChatStatus.Visibility = Visibility.Collapsed;
        }
        catch (Exception ex) { ShowChatStatus($"Didn't send. {Plain(ex)}"); return; }
        await PollChatAsync();
    }

    /// <summary>At most 1600 px on the long side; PNG keeps a screenshot sharp, JPEG when PNG would be big.
    /// The hub re-saves it anyway, so nothing hidden in the file goes along.</summary>
    private static byte[] Shrink(BitmapSource src)
    {
        var longest = Math.Max(src.PixelWidth, src.PixelHeight);
        BitmapSource bmp = new FormatConvertedBitmap(src, PixelFormats.Bgr24, null, 0);
        if (longest > 1600)
        {
            var k = 1600.0 / longest;
            bmp = new TransformedBitmap(bmp, new ScaleTransform(k, k));
        }
        byte[] Encode(BitmapEncoder enc)
        {
            enc.Frames.Add(BitmapFrame.Create(bmp));
            using var ms = new MemoryStream();
            enc.Save(ms);
            return ms.ToArray();
        }
        var png = Encode(new PngBitmapEncoder());
        return png.Length <= 3 * 1024 * 1024 ? png : Encode(new JpegBitmapEncoder { QualityLevel = 85 });
    }
}
