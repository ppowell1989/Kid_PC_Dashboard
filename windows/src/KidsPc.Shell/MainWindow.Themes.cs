using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace KidsPc.Shell;

/// <summary>The theme picker: one preview per theme in the library (Theme.All). Picking one changes the look
/// straight away and remembers it.</summary>
public partial class MainWindow
{
    private void OpenThemes(object s, RoutedEventArgs e)
    {
        BuildThemeChoices();
        ThemeOverlay.Visibility = Visibility.Visible;
    }

    private void CloseThemes(object s, RoutedEventArgs e) => ThemeOverlay.Visibility = Visibility.Collapsed;

    private void BuildThemeChoices()
    {
        ThemeChoices.Children.Clear();
        foreach (var t in Theme.All)
        {
            var choice = new Button { Content = Preview(t), Tag = t.Id == Theme.Current ? "on" : null };
            choice.SetResourceReference(StyleProperty, "Swatch");
            choice.Click += (_, _) => Theme.Apply(t.Id);
            ThemeChoices.Children.Add(choice);
        }
    }

    /// <summary>A tiny picture of the theme (its sky, a card with a line of "text", a button) with its name below.
    /// Drawn in the theme's own colours, whatever look is on now.</summary>
    private static UIElement Preview(ThemeInfo t)
    {
        static SolidColorBrush B(string hex) => new((Color)ColorConverter.ConvertFromString(hex));
        var card = new Border
        {
            Width = 112, Height = 64, CornerRadius = new CornerRadius(8), Background = B(t.Card),
            HorizontalAlignment = HorizontalAlignment.Left, VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(14, 14, 0, 0),
            Child = new StackPanel
            {
                Margin = new Thickness(10, 12, 10, 0),
                Children =
                {
                    new Border { Height = 8, Width = 70, CornerRadius = new CornerRadius(4), Background = B(t.Ink), HorizontalAlignment = HorizontalAlignment.Left },
                    new Border { Height = 6, Width = 46, CornerRadius = new CornerRadius(3), Background = B(t.Ink), Opacity = 0.45, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 8, 0, 0) },
                },
            },
        };
        var button = new Border
        {
            Width = 62, Height = 22, CornerRadius = new CornerRadius(7), Background = B(t.Accent),
            HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Bottom, Margin = new Thickness(0, 0, 14, 14),
        };
        var picture = new Border
        {
            Width = 196, Height = 118, CornerRadius = new CornerRadius(10), Background = B(t.Bg),
            Child = new Grid { Children = { card, button } },
        };
        var name = new TextBlock { Text = t.Name, FontSize = 19, Margin = new Thickness(2, 10, 0, 0) };
        name.SetResourceReference(TextBlock.FontFamilyProperty, "Display");
        var blurb = new TextBlock { Text = t.Blurb, FontSize = 14, Margin = new Thickness(2, 1, 0, 0) };
        blurb.SetResourceReference(TextBlock.ForegroundProperty, "Muted");
        return new StackPanel { Children = { picture, name, blurb } };
    }
}
