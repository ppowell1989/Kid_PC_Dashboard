using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>
/// The Family row on the home screen: one card per brother with what he is doing right now, a one-tap Join
/// into the same game (only when it is approved for this kid too; the hub decides), and the family voice room.
/// Colours and shapes come from the theme by resource name, so the row follows whichever look is picked.
/// </summary>
public partial class MainWindow
{
    private string _familySig = "";

    private void ShowFamilyPanel() =>
        FamilyPanel.Visibility = FamilyCards.Children.Count > 0 && CataloguePanel.Visibility != Visibility.Visible ? Visibility.Visible : Visibility.Collapsed;

    private void RedrawFamily()
    {
        _familySig = "";
        if (_state?.Rules is { } r) BuildFamily(r);
    }

    private void BuildFamily(Rules rules)
    {
        var family = rules.Family ?? [];
        var installed = _state!.InstalledGameIds;
        var sig = string.Join("|", family.Select(f => $"{f.Id}:{f.Online}:{f.Running}:{f.Locked}:{f.InFamilyVoice}:{f.JoinGame?.Id}:{f.AskRef}:{(f.JoinGame is { } g && installed.Contains(g.Id))}"))
                  + $"|voice:{rules.FamilyVoice}:{_voiceRoom}|asked:{_askedThisSession.Count}";
        if (sig == _familySig) return;
        _familySig = sig;
        FamilyCards.Children.Clear();
        foreach (var f in family) FamilyCards.Children.Add(FamilyCard(f, rules, installed));
        ShowFamilyPanel();
    }

    private UIElement FamilyCard(FamilyMember f, Rules rules, List<int> installed)
    {
        // Who: initial in a circle, with a green dot when his PC is on.
        var orb = new Grid { Width = 64, Height = 64, Margin = new Thickness(0, 0, 18, 0), VerticalAlignment = VerticalAlignment.Top };
        var circle = new Ellipse();
        circle.SetResourceReference(Shape.FillProperty, "AccentSoft");
        orb.Children.Add(circle);
        var initial = new TextBlock { Text = f.Name[..1].ToUpperInvariant(), FontSize = 30, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        initial.SetResourceReference(TextBlock.FontFamilyProperty, "Display");
        initial.SetResourceReference(TextBlock.ForegroundProperty, "Accent");
        orb.Children.Add(initial);
        var dot = new Ellipse { Width = 18, Height = 18, StrokeThickness = 3, HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Bottom };
        dot.SetResourceReference(Shape.FillProperty, f.Online && !f.Locked ? "Good" : "Thumb");
        dot.SetResourceReference(Shape.StrokeProperty, "Card");
        orb.Children.Add(dot);

        var name = new TextBlock { Text = f.Name, FontSize = 26 };
        name.SetResourceReference(TextBlock.FontFamilyProperty, "Display");
        var doing = !f.Online ? "His PC is off"
            : f.Locked ? "Done for today"
            : f.Running == "Chrome" ? "On the web"
            : f.Running.Length > 0 ? $"Playing {f.Running}"
            : "On the home screen";
        var status = new TextBlock { Text = doing, FontSize = 18, TextTrimming = TextTrimming.CharacterEllipsis, Margin = new Thickness(0, 2, 0, 0) };
        status.SetResourceReference(TextBlock.ForegroundProperty, "Muted");
        var head = new StackPanel();
        head.Children.Add(name);
        head.Children.Add(status);
        if (f.InFamilyVoice)
        {
            var badge = new Border { HorizontalAlignment = HorizontalAlignment.Left, Padding = new Thickness(10, 3, 10, 4), CornerRadius = new CornerRadius(10), Margin = new Thickness(0, 8, 0, 0) };
            badge.SetResourceReference(Border.BackgroundProperty, "AccentSoft");
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            var icon = new TextBlock { Text = "\uE767", FontSize = 14, Margin = new Thickness(0, 1, 8, 0), VerticalAlignment = VerticalAlignment.Center };   // speaker
            icon.SetResourceReference(TextBlock.FontFamilyProperty, "Icons");
            var bt = new TextBlock { Text = "In family voice", FontSize = 14, VerticalAlignment = VerticalAlignment.Center };
            foreach (var t in new[] { icon, bt }) { t.SetResourceReference(TextBlock.ForegroundProperty, "Accent"); row.Children.Add(t); }
            badge.Child = row;
            head.Children.Add(badge);
        }

        // What he can do: join the game, get it, ask for it, and talk.
        var actions = new WrapPanel { Margin = new Thickness(0, 16, 0, 0) };
        if (f.JoinGame is { } g)
        {
            actions.Children.Add(installed.Contains(g.Id)
                ? ActionButton($"Join {g.Title}", () => JoinGame(f, g))
                : ActionButton($"Get {g.Title}", async () => await InstallAsync(g)));
        }
        else if (f.GameTitle.Length > 0 && f.AskRef.Length > 0 && !f.Locked)
        {
            var asked = AlreadyAsked(f.GameTitle);
            actions.Children.Add(ActionButton(asked ? "Asked, waiting" : $"Ask to play {f.GameTitle}", async () =>
            {
                _askedThisSession.Add(f.GameTitle);
                await SendRequest("game", f.GameTitle, $"Sent! Mom or Dad will look at \"{f.GameTitle}\".", reference: f.AskRef);
                RedrawFamily();
            }, enabled: !asked));
        }
        if (rules.FamilyVoice)
        {
            var inRoom = _voiceRoom == "family";
            actions.Children.Add(ActionButton(inRoom ? "You're in family voice" : f.InFamilyVoice ? "Join family voice" : "Talk", async () => await OpenVoiceAsync(family: true),
                quiet: actions.Children.Count > 0, enabled: !inRoom));
        }

        var body = new DockPanel();
        DockPanel.SetDock(orb, Dock.Left);
        body.Children.Add(orb);
        var right = new StackPanel();
        right.Children.Add(head);
        if (actions.Children.Count > 0) right.Children.Add(actions);
        body.Children.Add(right);

        var card = new Grid { Width = 560, Margin = new Thickness(0, 0, 20, 0) };
        var shadow = new Border();
        shadow.SetResourceReference(StyleProperty, "Shadow");
        var box = new Border { Padding = new Thickness(24, 20, 20, 20), Child = body };
        box.SetResourceReference(StyleProperty, "CardBox");
        card.Children.Add(shadow);
        card.Children.Add(box);
        return card;
    }

    private static Button ActionButton(string text, Action onClick, bool quiet = false, bool enabled = true)
    {
        var b = new Button { Content = text, IsEnabled = enabled, Margin = new Thickness(0, 0, 10, 8) };
        b.SetResourceReference(StyleProperty, "Pill");
        if (quiet)
        {
            b.SetResourceReference(BackgroundProperty, "Card");
            b.SetResourceReference(ForegroundProperty, "Text");
            b.SetResourceReference(BorderBrushProperty, "Line");
        }
        b.Click += (_, _) => onClick();
        return b;
    }

    /// <summary>Starts the same game his brother is in. Getting into the same match is then up to the game
    /// itself (Steam friends, a party code); the shell gets both of them into the game and the voice room.</summary>
    private void JoinGame(FamilyMember f, Game g)
    {
        if (Launcher.LaunchGame(g))
            StatusLine.Text = $"Starting {g.Title}. {f.Name} is already in there!";
    }
}
