using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>Family chat docked in the bottom-right corner: Mom, Dad and both kids in one conversation.</summary>
public partial class MainWindow
{
    private readonly DispatcherTimer _chatPoll = new() { Interval = TimeSpan.FromSeconds(4) };
    private long _chatLast, _chatSeen;
    private bool _chatStarted, _chatBusy;
    private int _chatTick;

    private bool ChatOpen => ChatPanel.Visibility == Visibility.Visible;

    private void StartChat()
    {
        // Poll every 4 s while open, every 12 s while closed (for the unread badge).
        _chatPoll.Tick += async (_, _) => { if (ChatOpen || ++_chatTick % 3 == 0) await PollChatAsync(); };
        _chatPoll.Start();
        _ = PollChatAsync();
    }

    private async Task PollChatAsync()
    {
        if (_chatBusy) return;
        _chatBusy = true;
        try
        {
            var msgs = await _helper.GetAsync<List<ChatMessage>>(new PipeRequest("chat", Detail: _chatLast.ToString())) ?? [];
            ChatStatus.Visibility = Visibility.Collapsed;
            if (msgs.Count == 0) { _chatStarted = true; return; }
            var atBottom = ChatScroll.VerticalOffset >= ChatScroll.ScrollableHeight - 30;
            foreach (var m in msgs) ChatLog.Children.Add(Bubble(m));
            while (ChatLog.Children.Count > 200) ChatLog.Children.RemoveAt(0);
            _chatLast = msgs[^1].Id;
            if (!_chatStarted) _chatSeen = _chatLast;   // history isn't "new"
            _chatStarted = true;
            if (ChatOpen) _chatSeen = _chatLast;
            if (atBottom || ChatOpen) ChatScroll.ScrollToEnd();
            UpdateBadge(msgs);
        }
        catch
        {
            ChatStatus.Text = "Chat can't reach the home server right now.";
            ChatStatus.Visibility = Visibility.Visible;
        }
        finally { _chatBusy = false; }
    }

    private int _unread;
    private void UpdateBadge(List<ChatMessage> fresh)
    {
        _unread = ChatOpen ? 0 : _unread + fresh.Count(m => m.Id > _chatSeen && !m.Mine);
        ChatBadge.Visibility = _unread > 0 ? Visibility.Visible : Visibility.Collapsed;
        ChatBadgeText.Text = _unread > 9 ? "9+" : _unread.ToString();
    }

    private UIElement Bubble(ChatMessage m)
    {
        var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 8), HorizontalAlignment = m.Mine ? HorizontalAlignment.Right : HorizontalAlignment.Left, MaxWidth = 300 };
        if (!m.Mine)
            stack.Children.Add(new TextBlock { Text = m.Sender, FontSize = 12, Foreground = (Brush)FindResource("Muted"), Margin = new Thickness(4, 0, 0, 2) });
        var bg = m.Mine ? (Brush)FindResource("Accent")
               : m.KidId is null ? Brushes.White                                           // Mom or Dad
               : new SolidColorBrush(Color.FromRgb(0xDD, 0xF4, 0xEA));                    // brother
        stack.Children.Add(new Border
        {
            Background = bg, CornerRadius = new CornerRadius(14), Padding = new Thickness(12, 7, 12, 8),
            HorizontalAlignment = stack.HorizontalAlignment,
            Child = new TextBlock { Text = m.Text, TextWrapping = TextWrapping.Wrap, FontSize = 16, Foreground = m.Mine ? Brushes.White : (Brush)FindResource("Text") },
        });
        if (DateTimeOffset.TryParse(m.At, out var at))
            stack.Children.Add(new TextBlock { Text = at.ToString("h:mm tt"), FontSize = 11, Foreground = (Brush)FindResource("Muted"), Margin = new Thickness(4, 2, 4, 0), HorizontalAlignment = stack.HorizontalAlignment });
        return stack;
    }

    private async void ToggleChat(object s, RoutedEventArgs e)
    {
        var open = !ChatOpen;
        ChatPanel.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        ChatPill.Visibility = open ? Visibility.Collapsed : Visibility.Visible;
        if (!open) return;
        _chatSeen = _chatLast;
        UpdateBadge([]);
        ChatScroll.ScrollToEnd();
        ChatBox.Focus();
        await PollChatAsync();
    }

    private async void ChatKeyDown(object s, KeyEventArgs e)
    {
        if (e.Key == Key.Enter) { e.Handled = true; await SendChatAsync(); }
    }

    private async void SendChat(object s, RoutedEventArgs e) => await SendChatAsync();

    private async Task SendChatAsync()
    {
        var text = ChatBox.Text.Trim();
        if (text.Length == 0) return;
        ChatBox.Text = "";
        try
        {
            await _helper.GetAsync<object>(new PipeRequest("chat_send", Detail: text));
        }
        catch (Exception ex)
        {
            ChatBox.Text = text;
            ChatStatus.Text = $"Didn't send: {ex.Message}";
            ChatStatus.Visibility = Visibility.Visible;
            return;
        }
        await PollChatAsync();
    }
}
