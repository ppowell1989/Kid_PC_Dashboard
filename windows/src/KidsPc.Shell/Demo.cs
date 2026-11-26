using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using KidsPc.Shared;

namespace KidsPc.Shell;

/// <summary>What the shell asks the helper for. The real one talks over the named pipe.</summary>
internal interface IHelper
{
    Task<T?> GetAsync<T>(PipeRequest req, CancellationToken ct = default);
}

internal sealed class PipeHelper : IHelper
{
    private readonly HelperClient _client = new();
    public Task<T?> GetAsync<T>(PipeRequest req, CancellationToken ct = default) => _client.GetAsync<T>(req, ct);
}

/// <summary>
/// Stand-in helper for "KidsPcShell.exe --demo &lt;scene&gt;": canned data, no hub, no helper, nothing launches.
/// Used by windows\tests\Screenshots.ps1 so every screen of the design can be looked at on real Windows pixels.
/// Scenes: home, catalogue, ask, chat, chat-friend, voice, family, warning, lock, themes, power. Add "--theme &lt;id&gt;" to see one in another look.
/// </summary>
internal sealed class DemoHelper(string scene) : IHelper
{
    private const string Art = "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{0}/header.jpg";
    private static string ArtFor(int appid) => string.Format(Art, appid);

    private static readonly (int Appid, string Title, string Rating)[] Catalogue =
    [
        (291550, "Brawlhalla", "E10+"),
        (620, "Portal 2", "E10+"),
        (413150, "Stardew Valley", "E10+"),
        (105600, "Terraria", "T"),
        (252950, "Rocket League", "E"),
        (920210, "LEGO Star Wars: The Skywalker Saga", "E10+"),
        (1097150, "Fall Guys", "E"),
    ];

    /// <summary>Scenes "art" and "art-catalogue": the hub's real game art for every Battle.net and Epic game, from the
    /// JSON file in KIDSPC_DEMO_ART (hub/tests/art_demo.py writes it), so CI shows each picture on real Windows tiles.</summary>
    private sealed record DemoArt(List<Game> Games, Discover Discover);
    private readonly DemoArt? _art = scene.StartsWith("art") && Environment.GetEnvironmentVariable("KIDSPC_DEMO_ART") is { Length: > 0 } f && File.Exists(f)
        ? System.Text.Json.JsonSerializer.Deserialize<DemoArt>(File.ReadAllText(f), Json.Options) : null;

    public Task<T?> GetAsync<T>(PipeRequest req, CancellationToken ct = default)
    {
        object? result = req.Op switch
        {
            "state" => State(),
            "game_search" => new List<GameHit>
            {
                new("steam:427520", "Factorio", ArtFor(427520), "E10+", "Build and maintain factories on an alien planet. Mine resources, research technology and automate production."),
                new("steam:544550", "Stationeers", ArtFor(544550), "", "Construct and manage your own space station."),
                new("steam:220200", "Kerbal Space Program", ArtFor(220200), "E", "Take charge of the space program for the alien race known as the Kerbals. Build spacecraft that fly (or don't) based on realistic physics."),
                new("steam:275850", "No Man's Sky", ArtFor(275850), "T", "A game about exploration and survival in an infinite procedurally generated universe."),
            },
            "game_discover" => _art?.Discover ?? DemoShelves(),
            "buy_time" => new BoughtTime(15, 10, 32),
            "chat_convs" => new ChatList(
            [
                new("family", "Family", "family", 5, 4, true),
                new("kids", "Barrett", "kids", 12, 12, scene != "lock"),
                new("friend-demo", "Max", "friend", 25, 25, scene != "lock", scene == "lock" ? "Off while the PC is locked" : ""),
            ], true, true, true),
            "chat" => req.Detail == "0" ? Chat(req.Ref ?? "family") : new List<ChatMessage>(),
            "chat_picture_get" => new ChatPicture(DemoPicture()),
            "chat_send" or "chat_picture" => new { sent = true },
            "install" => new InstallResult("none", null, "Demo mode: nothing was installed."),
            "voice_token" or "family_voice_token" => scene is "voice" or "family" ? new VoiceToken("demo", "demo", "demo") : throw new HelperSaidNo("Voice chat is off in demo mode."),
            _ => null,
        };
        return Task.FromResult((T?)result);
    }

    private ShellState State()
    {
        var time = scene switch
        {
            "lock" => new TimeState(90, 0, 90, 0, 150, true, "time", false),
            "warning" => new TimeState(90, 15, 97, 8, 150, false, "", true),
            _ => new TimeState(90, 0, 28, 62, 150, false, "", false),
        };
        // Home screen order follows play: Stardew today, Brawlhalla yesterday, Portal 2 last week, Terraria new.
        var played = new Dictionary<int, string>
        {
            [413150] = DateTimeOffset.Now.AddHours(-2).ToString("o"),
            [291550] = DateTimeOffset.Now.AddDays(-1).ToString("o"),
            [620] = DateTimeOffset.Now.AddDays(-3).ToString("o"),
        };
        var games = _art?.Games ?? Catalogue.Select((g, i) => new Game(i + 1, g.Title, "steam", g.Appid.ToString(), g.Rating, ArtFor(g.Appid),
            played.GetValueOrDefault(g.Appid, ""), New: g.Appid == 105600)).ToList();
        var rules = new Rules(new KidInfo("adam", "Adam"), time, 10, games, new WebRules("filtered", []), false,
            scene == "lock" ? new List<PendingRequest> { new(1, "time", "15", "pending", DateTime.UtcNow.ToString("u")) }
                : new List<PendingRequest> { new(2, "game", "Minecraft", "pending", DateTime.UtcNow.ToString("u")), new(3, "game", "Golf With Your Friends", "pending", DateTime.UtcNow.ToString("u")) },
            [new Friend("demo", "Max")],
            Family: [new FamilyMember("barrett", "Barrett", true, "Brawlhalla", false, InFamilyVoice: scene == "family",
                GameTitle: "Brawlhalla", JoinGame: games[0], AskRef: "")],
            Points: new PointsOffer(42, 10, 15, true, "", 30));   // Skylight chore points he can spend on time
        var messages = scene == "warning" ? new List<ParentMessage> { new(1, "Mom", "Dinner in 10 minutes!", DateTimeOffset.Now) } : null;
        // Two games a parent approved, installing by themselves (the catalogue shows how far along).
        if (_art is not null) return new ShellState(rules, games.Select(g => g.Id).ToList(), time.Locked, true, DateTimeOffset.Now, null);
        return new ShellState(rules, [1, 2, 3, 4], time.Locked, true, DateTimeOffset.Now, messages,
            Installing: new Dictionary<int, string> { [5] = "Downloading 45%", [6] = "Waiting for Steam" },
            InstallPercent: new Dictionary<int, int> { [5] = 45 });
    }

    /// <summary>"Get more games" shelves, as the hub sends them: his brother's games, then ideas up to E10+.</summary>
    private static Discover DemoShelves()
    {
        GameHit H(int appid, string name, string esrb, string blurb = "", bool asked = false) =>
            new($"steam:{appid}", name, ArtFor(appid), esrb, blurb, "Steam", asked);
        return new Discover("E10+",
        [
            new("Barrett has these", [H(477160, "Human: Fall Flat", "E10+", "A physics puzzle game for one to eight players.")]),
            new("Build and create",
            [
                H(220200, "Kerbal Space Program", "E", "Build spacecraft that fly (or don't) based on realistic physics."),
                H(493340, "Planet Coaster", "E"), H(703080, "Planet Zoo", "E"), H(1291340, "Townscaper", "E"),
                H(433340, "Slime Rancher", "E10+"), H(427520, "Factorio", "E10+"),
            ]),
            new("Play together",
            [
                H(728880, "Overcooked! 2", "E"), H(996770, "Moving Out", "E"), H(1016920, "Unrailed!", "E"),
                H(431240, "Golf With Your Friends", "E", asked: true), H(1097150, "Fall Guys", "E"),
            ]),
            new("Puzzles and adventures",
            [
                H(1055540, "A Short Hike", "E"), H(837470, "Untitled Goose Game", "E"), H(736260, "Baba Is You", "E"),
                H(387290, "Ori and the Blind Forest: Definitive Edition", "E"), H(504230, "Celeste", "E10+"), H(1135690, "Unpacking", "E10+"),
            ]),
        ]);
    }

    private static List<ChatMessage> Chat(string conv)
    {
        var now = DateTimeOffset.Now;
        string At(int minutesAgo) => now.AddMinutes(-minutesAgo).ToString("o");
        return conv switch
        {
            "kids" =>
            [
                new(10, "Barrett", "barrett", "are you on yet", At(20), false, "kids"),
                new(11, "Adam", "adam", "after homework. 20 min", At(18), true, "kids"),
                new(12, "Barrett", "barrett", "ok I'll make the server", At(17), false, "kids"),
            ],
            "friend-demo" =>
            [
                new(20, "Max", null, "did you beat the boss yet", At(15), false, "friend-demo", Friend: true),
                new(21, "Adam", "adam", "YES look", At(12), true, "friend-demo", Picture: true),
                new(22, "Max", null, "no way!! how", At(11), false, "friend-demo", Friend: true),
                new(23, "Max", null, "this guide helped me www.example.com/boss-guide", At(9), false, "friend-demo", Friend: true,
                    Links: ["www.example.com/boss-guide"]),
                new(24, "Adam", "adam", "cool, play at 4?", At(8), true, "friend-demo"),
                new(25, "Max", null, "my best goal ever", At(5), false, "friend-demo", Friend: true, Picture: true, Video: true),
            ],
            _ =>
            [
                new(1, "Dad", null, "Homework done?", At(42), false),
                new(2, "Adam", "adam", "Yes! Can I play Brawlhalla with Barrett?", At(40), true),
                new(3, "Barrett", "barrett", "pleeease", At(39), false),
                new(4, "Mom", null, "OK, until dinner. Be nice to each other.", At(37), false),
                new(5, "Adam", "adam", "Thanks Mom!", At(36), true),
            ],
        };
    }

    /// <summary>A made-up "game screenshot" for the demo chat: a sky, hills and a banner.</summary>
    private static string DemoPicture()
    {
        var v = new DrawingVisual();
        using (var dc = v.RenderOpen())
        {
            dc.DrawRectangle(new LinearGradientBrush(Color.FromRgb(0x5B, 0x8D, 0xEF), Color.FromRgb(0xB8, 0xE1, 0xFF), 90), null, new Rect(0, 0, 640, 360));
            dc.DrawEllipse(new SolidColorBrush(Color.FromRgb(0x4C, 0xAF, 0x50)), null, new Point(160, 420), 320, 160);
            dc.DrawEllipse(new SolidColorBrush(Color.FromRgb(0x38, 0x8E, 0x3C)), null, new Point(520, 440), 300, 170);
            dc.DrawRoundedRectangle(new SolidColorBrush(Color.FromArgb(0xD0, 0x1A, 0x1A, 0x2E)), null, new Rect(150, 110, 340, 90), 18, 18);
            var t = new FormattedText("BOSS DEFEATED!", System.Globalization.CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
                new Typeface("Segoe UI Black"), 40, Brushes.Gold, 1.0);
            dc.DrawText(t, new Point(320 - t.Width / 2, 155 - t.Height / 2));
        }
        var bmp = new RenderTargetBitmap(640, 360, 96, 96, PixelFormats.Pbgra32);
        bmp.Render(v);
        var enc = new PngBitmapEncoder();
        enc.Frames.Add(BitmapFrame.Create(bmp));
        using var ms = new MemoryStream();
        enc.Save(ms);
        return Convert.ToBase64String(ms.ToArray());
    }
}
