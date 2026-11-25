using System.Windows;
using System.Windows.Controls;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>
/// "Get more games" beyond the search box: the games he asked for that are waiting on a parent, and shelves to
/// browse (what his brother has, then hand-picked ideas up to the rating a parent set on the hub). Tapping a game
/// on a shelf opens the same "Ask Mom or Dad?" card as a search result. Nothing on a shelf is approved yet.
/// </summary>
public partial class MainWindow
{
    private Discover? _discover;
    private DateTimeOffset _discoverAt = DateTimeOffset.MinValue;
    private bool _discoverLoading;
    private readonly Dictionary<string, DateTimeOffset> _justAsked = new(StringComparer.OrdinalIgnoreCase);
    private static readonly TimeSpan DiscoverFresh = TimeSpan.FromMinutes(10);

    /// <summary>Fetch the shelves when "Get more games" opens (at most every 10 minutes), or right after he asked for one.</summary>
    private async Task LoadDiscoverAsync(bool force = false)
    {
        if (_discoverLoading || (!force && DateTimeOffset.Now - _discoverAt < DiscoverFresh)) return;
        _discoverLoading = true;
        try
        {
            _discover = await _helper.GetAsync<Discover>(new PipeRequest("game_discover"));
            _discoverAt = DateTimeOffset.Now;
        }
        catch { /* an older hub has no shelves, or it is unreachable: the page still has search */ }
        finally { _discoverLoading = false; }
        BuildShelves();
    }

    private void BuildShelves()
    {
        Shelves.Children.Clear();
        foreach (var shelf in _discover?.Shelves ?? [])
        {
            if (shelf.Games.Count == 0) continue;
            var heading = new TextBlock { Text = shelf.Title, Margin = new Thickness(0, 8, 0, 14) };
            heading.SetResourceReference(StyleProperty, "Heading");
            var row = new WrapPanel();
            foreach (var g in shelf.Games.Take(12))
            {
                var asked = g.Asked || (_justAsked.TryGetValue(g.Name, out var at) && DateTimeOffset.Now - at < DiscoverFresh);
                var facts = asked ? "Asked, waiting" : HitFacts(g);
                row.Children.Add(Tile(facts.Length == 0 ? g.Name : $"{g.Name}\n{facts}", () => ShowAsk(g), enabled: !asked, image: g.ImageUrl, small: true));
            }
            Recolour(row);
            var block = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };
            block.Children.Add(heading);
            block.Children.Add(row);
            Shelves.Children.Add(block);
        }
    }

    /// <summary>"Steam · Rated E10+": where it would come from and its rating, for a tile's caption.</summary>
    private static string HitFacts(GameHit g) =>
        string.Join(" · ", new[] { g.Store, string.IsNullOrEmpty(g.Esrb) ? null : $"Rated {g.Esrb}" }.Where(x => !string.IsNullOrEmpty(x)));

    /// <summary>The games he asked for that a parent hasn't answered yet, as small labels.</summary>
    private void BuildAsked(Rules rules)
    {
        // Ones he sent in the last couple of minutes show straight away, before the hub's list catches up.
        var recent = _justAsked.Where(kv => DateTimeOffset.Now - kv.Value < TimeSpan.FromMinutes(2)).Select(kv => kv.Key);
        var asked = rules.PendingRequests.Where(r => r.Kind == "game").Select(r => r.Detail)
            .Concat(recent).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        AskedChips.Children.Clear();
        foreach (var title in asked)
        {
            var label = new StackPanel { Orientation = Orientation.Horizontal };
            var icon = new TextBlock { Text = "", FontSize = 15, Margin = new Thickness(0, 2, 8, 0), VerticalAlignment = VerticalAlignment.Center };
            icon.SetResourceReference(TextBlock.FontFamilyProperty, "Icons");
            icon.SetResourceReference(TextBlock.ForegroundProperty, "Accent");
            label.Children.Add(icon);
            label.Children.Add(new TextBlock { Text = title, FontSize = 17, VerticalAlignment = VerticalAlignment.Center });
            var chip = new Border { Child = label, Padding = new Thickness(16, 9, 18, 10), Margin = new Thickness(0, 0, 10, 10), BorderThickness = new Thickness(2) };
            chip.SetResourceReference(Border.BackgroundProperty, "Card");
            chip.SetResourceReference(Border.BorderBrushProperty, "Line");
            chip.SetResourceReference(Border.CornerRadiusProperty, "RButton");
            AskedChips.Children.Add(chip);
        }
        AskedPanel.Visibility = asked.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SearchTextChanged(object s, TextChangedEventArgs e) =>
        SearchHint.Visibility = AskGameBox.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;

    private void ClearSearch(object s, RoutedEventArgs e)
    {
        AskGameBox.Clear();
        SearchResults.Children.Clear();
        SearchStatus.Text = "";
        SearchHead.Visibility = Visibility.Collapsed;
    }

    private void RecolourShelves()
    {
        foreach (var block in Shelves.Children.OfType<StackPanel>())
            foreach (var row in block.Children.OfType<WrapPanel>()) Recolour(row);
    }
}
