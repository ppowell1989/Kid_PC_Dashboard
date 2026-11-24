using System.Media;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;

namespace KidsPc.Shell;

/// <summary>
/// A small card in the top right corner when someone in the family writes in the chat while it is closed:
/// who wrote and the first words, with a sound. It floats over a game without taking the keyboard away from
/// it, goes away by itself, and a click on it opens the chat. This is the kid screen's own card, not a Windows
/// notification (those are switched off for his account).
/// </summary>
public sealed class ChatToast : Window
{
    private static ChatToast? _showing;
    private readonly DispatcherTimer _hide = new() { Interval = TimeSpan.FromSeconds(8) };

    private ChatToast(FrameworkElement look, string sender, string text, Action onClick)
    {
        WindowStyle = WindowStyle.None;
        AllowsTransparency = true;
        Background = Brushes.Transparent;
        ResizeMode = ResizeMode.NoResize;
        ShowInTaskbar = false;
        ShowActivated = false;      // never steals the keyboard from a game
        Focusable = false;
        Topmost = true;
        SizeToContent = SizeToContent.WidthAndHeight;
        Cursor = Cursors.Hand;

        Brush Paint(string key, Color fallback) => look.TryFindResource(key) as Brush ?? new SolidColorBrush(fallback);
        var who = new TextBlock { Text = $"{sender} wrote in Family chat", FontSize = 14, Foreground = Paint("Muted", Colors.DimGray) };
        var words = new TextBlock
        {
            Text = text.Length > 140 ? text[..140] + "…" : text, FontSize = 19, TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 4, 0, 0), Foreground = Paint("Text", Colors.Black),
        };
        if (look.TryFindResource("Body") is FontFamily body) who.FontFamily = words.FontFamily = body;
        Content = new Border
        {
            Width = 380, Margin = new Thickness(12), Padding = new Thickness(18, 14, 18, 16), CornerRadius = new CornerRadius(14),
            Background = Paint("Card", Colors.White), BorderBrush = Paint("Accent", Colors.SeaGreen), BorderThickness = new Thickness(3),
            Child = new StackPanel { Children = { who, words } },
        };

        MouseLeftButtonUp += (_, _) => { Close(); onClick(); };
        _hide.Tick += (_, _) => Close();
        Closed += (_, _) => { _hide.Stop(); if (_showing == this) _showing = null; };
        Loaded += (_, _) =>
        {
            var area = SystemParameters.WorkArea;
            Left = area.Right - ActualWidth - 20;
            Top = area.Top + 20;
            _hide.Start();
        };
    }

    /// <summary>Shows the card (replacing one that is still up) and plays a sound.</summary>
    public static void Show(FrameworkElement look, string sender, string text, Action onClick)
    {
        try
        {
            _showing?.Close();
            _showing = new ChatToast(look, sender, text, onClick);
            _showing.Show();
            SystemSounds.Asterisk.Play();
        }
        catch { /* the badge on the Family chat button still shows it */ }
    }
}
