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
        // Poll every 4 s while open, every 8 s while closed (for the unread badge and the new-message card).
        _chatPoll.Tick += async (_, _) => { if (ChatOpen || ++_chatTick % 2 == 0) await PollChatAsync(); };
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
            var live = _chatStarted;                    // false on the first load: history isn't "new"
            // Someone else wrote while the chat is closed (or a game is covering it): show it and make a sound.
            var news = live ? msgs.LastOrDefault(m => m.Id > _chatSeen && !m.Mine) : null;
            if (news is not null && !(ChatOpen && IsActive))
                ChatToast.Show(this, news.Sender, news.Text, () => { BringToFront(); if (!ChatOpen) ToggleChat(this, new RoutedEventArgs()); });
            if (!_chatStarted) _chatSeen = _chatLast;
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
        // Colours are tied to the theme by name, so old bubbles follow when the kid picks another look.
        var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 8), HorizontalAlignment = m.Mine ? HorizontalAlignment.Right : HorizontalAlignment.Left, MaxWidth = 300 };
        TextBlock Small(string text, Thickness margin)
        {
            var t = new TextBlock { Text = text, FontSize = 12, Margin = margin, HorizontalAlignment = stack.HorizontalAlignment };
            t.SetResourceReference(TextBlock.ForegroundProperty, "Muted");
            return t;
        }
        if (!m.Mine) stack.Children.Add(Small(m.Sender, new Thickness(4, 0, 0, 2)));
        var words = new TextBlock { Text = m.Text, TextWrapping = TextWrapping.Wrap, FontSize = 16 };
        words.SetResourceReference(TextBlock.ForegroundProperty, m.Mine ? "OnAccent" : "BubbleText");
        var bubble = new Border { CornerRadius = new CornerRadius(14), Padding = new Thickness(12, 7, 12, 8), HorizontalAlignment = stack.HorizontalAlignment, Child = words };
        bubble.SetResourceReference(Border.BackgroundProperty, m.Mine ? "Accent" : m.KidId is null ? "BubbleParent" : "BubbleSibling");   // me / Mom or Dad / brother
        stack.Children.Add(bubble);
        if (DateTimeOffset.TryParse(m.At, out var at)) stack.Children.Add(Small(at.ToString("h:mm tt"), new Thickness(4, 2, 4, 0)));
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
            ChatStatus.Text = $"Didn't send. {Plain(ex)}";
            ChatStatus.Visibility = Visibility.Visible;
            return;
        }
        await PollChatAsync();
    }
}
