using System.IO;
using System.Windows;

namespace KidsPc.Shell;

/// <summary>One entry in the theme library. The colours here are only for the little preview in the picker;
/// the theme itself is Themes\&lt;File&gt;.xaml.</summary>
public sealed record ThemeInfo(string Id, string Name, string Blurb, string File, string Bg, string Card, string Accent, string Ink);

/// <summary>
/// The kid's choice of look. Each theme is one XAML file of colours, fonts and corner sizes; switching swaps that
/// file in the app's resources and everything on screen follows at once. The choice is remembered per Windows
/// account in %LOCALAPPDATA%\KidsPc\theme.txt. A theme changes looks only, never rules.
/// </summary>
public static class Theme
{
    public static readonly ThemeInfo[] All =
    [
        new("clean",    "Clean",       "Light and calm",            "Clean",    "#EEF3FB", "#FFFFFF", "#3D6BF5", "#18213A"),
        new("blocks",   "Blocks",      "A blocky world to build in", "Blocks",   "#7EC8F2", "#F3ECDC", "#4E9A2E", "#2B2118"),
        new("royale",   "Royale",      "Loud, bright, party time",  "Royale",   "#5A28C8", "#1B1464", "#FFD400", "#FFFFFF"),
        new("speedway", "Speedway",    "Dark and fast",             "Speedway", "#171A20", "#272D38", "#FF6A00", "#F2F4F7"),
        new("retro",    "Retro space", "Neon planets, 1990s style", "Retro",    "#0B0628", "#1A1060", "#FF2E97", "#FFD23F"),
    ];

    public static string Current { get; private set; } = All[0].Id;

    /// <summary>Raised after the look has changed, so code-drawn parts (backdrop, tiles) can redraw.</summary>
    public static event Action? Changed;

    private static string SavePath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "KidsPc", "theme.txt");

    /// <summary>At startup: the theme named on the command line (demo runs), else the saved one, else the default.</summary>
    public static void Load(string? forced)
    {
        var id = forced;
        if (id is null) { try { id = File.ReadAllText(SavePath).Trim(); } catch { /* never chosen yet */ } }
        Apply(id ?? All[0].Id, save: false);
    }

    public static void Apply(string id, bool save = true)
    {
        var theme = All.FirstOrDefault(t => t.Id == id) ?? All[0];
        if (theme.Id != All[0].Id || Current != All[0].Id)   // the default is already in App.xaml at startup
        {
            var look = new ResourceDictionary { Source = new Uri($"/KidsPcShell;component/Themes/{theme.File}.xaml", UriKind.Relative) };
            var merged = Application.Current.Resources.MergedDictionaries;
            merged.Clear();
            merged.Add(look);
        }
        Current = theme.Id;
        if (save && App.DemoScene is null)
        {
            try { Directory.CreateDirectory(Path.GetDirectoryName(SavePath)!); File.WriteAllText(SavePath, theme.Id); }
            catch { /* the look still changes for this session */ }
        }
        Changed?.Invoke();
    }
}
