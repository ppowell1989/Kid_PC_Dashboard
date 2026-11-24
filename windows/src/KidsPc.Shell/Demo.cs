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
/// Scenes: home, catalogue, ask, chat, voice, family, warning, lock, themes, power. Add "--theme &lt;id&gt;" to see one in another look.
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
            "chat" => req.Detail == "0" ? Chat() : new List<ChatMessage>(),
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
        var games = Catalogue.Select((g, i) => new Game(i + 1, g.Title, "steam", g.Appid.ToString(), g.Rating, ArtFor(g.Appid))).ToList();
        var rules = new Rules(new KidInfo("adam", "Adam"), time, 10, games, new WebRules("filtered", []), false,
            scene == "lock" ? new List<PendingRequest> { new(1, "time", "15", "pending", DateTime.UtcNow.ToString("u")) } : new List<PendingRequest>(),
            [new Friend("demo", "Max")],
            Family: [new FamilyMember("barrett", "Barrett", true, "Brawlhalla", false, InFamilyVoice: scene == "family",
                GameTitle: "Brawlhalla", JoinGame: games[0], AskRef: "")]);
        var messages = scene == "warning" ? new List<ParentMessage> { new(1, "Mom", "Dinner in 10 minutes!", DateTimeOffset.Now) } : null;
        // Two games a parent approved, installing by themselves (the catalogue shows how far along).
        return new ShellState(rules, [1, 2, 3, 4], time.Locked, true, DateTimeOffset.Now, messages,
            Installing: new Dictionary<int, string> { [5] = "Downloading 45%", [6] = "Waiting for Steam" },
            InstallPercent: new Dictionary<int, int> { [5] = 45 });
    }

    private static List<ChatMessage> Chat()
    {
        var now = DateTimeOffset.Now;
        string At(int minutesAgo) => now.AddMinutes(-minutesAgo).ToString("o");
        return
        [
            new(1, "Dad", null, "Homework done?", At(42), false),
            new(2, "Adam", "adam", "Yes! Can I play Brawlhalla with Barrett?", At(40), true),
            new(3, "Barrett", "barrett", "pleeease", At(39), false),
            new(4, "Mom", null, "OK, until dinner. Be nice to each other.", At(37), false),
            new(5, "Adam", "adam", "Thanks Mom!", At(36), true),
        ];
    }
}
