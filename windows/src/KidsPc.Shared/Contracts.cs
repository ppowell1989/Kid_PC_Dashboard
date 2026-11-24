using System.Text.Json;
using System.Text.Json.Serialization;

namespace KidsPc.Shared;

// Mirrors the hub's /api/device/rules response (hub/app/main.py).

public sealed record KidInfo(string Id, string Name);

public sealed record TimeState(
    int Allowance, int Granted, int Used,
    [property: JsonPropertyName("minutes_left")] int MinutesLeft,
    [property: JsonPropertyName("minutes_to_bedtime")] int MinutesToBedtime,
    bool Locked,
    [property: JsonPropertyName("lock_reason")] string LockReason,   // "", time, bedtime, outside_hours, paused
    bool Warn,
    [property: JsonPropertyName("minutes_to_stop")] int MinutesToStop = 0,   // until the play window ends or bedtime
    [property: JsonPropertyName("next_play")] string? NextPlay = null);      // ISO local time the next play window opens

public sealed record Game(
    int Id, string Title, string Launcher,
    [property: JsonPropertyName("launch_ref")] string LaunchRef,
    string Rating,
    [property: JsonPropertyName("image_url")] string ImageUrl);

public sealed record WebRules(string Mode, [property: JsonPropertyName("allowed_sites")] List<string> AllowedSites, DnsRules? Dns = null, SearchRules? Search = null);

/// <summary>Chrome's default search engine plus URL patterns that force searches through it.</summary>
public sealed record SearchRules(string Name, string Url, List<string> Block, List<string> Allow);

/// <summary>servers=[] leaves the PC's DNS alone; doh="" turns Chrome's secure DNS off.</summary>
public sealed record DnsRules(string Filter, List<string> Servers, string Doh);

public sealed record PendingRequest(int Id, string Kind, string Detail, string State,
    [property: JsonPropertyName("created_at")] string CreatedAt);

public sealed record Friend(string Code, [property: JsonPropertyName("friend_name")] string FriendName);

/// <summary>A brother, as the Family row shows him. JoinGame is set only when his game is approved for this kid too.</summary>
public sealed record FamilyMember(string Id, string Name, bool Online, string Running, bool Locked,
    [property: JsonPropertyName("in_family_voice")] bool InFamilyVoice,
    [property: JsonPropertyName("game_title")] string GameTitle,
    [property: JsonPropertyName("join_game")] Game? JoinGame,
    [property: JsonPropertyName("ask_ref")] string AskRef);

public sealed record Rules(
    KidInfo Kid,
    TimeState Time,
    [property: JsonPropertyName("warning_minutes")] int WarningMinutes,
    List<Game> Games,
    WebRules Web,
    [property: JsonPropertyName("game_pass_enabled")] bool GamePassEnabled,
    [property: JsonPropertyName("pending_requests")] List<PendingRequest> PendingRequests,
    List<Friend> Friends,
    [property: JsonPropertyName("managed_apps")] List<string>? ManagedApps = null,
    List<HubCommand>? Commands = null,
    List<FamilyMember>? Family = null,
    [property: JsonPropertyName("family_voice")] bool FamilyVoice = true,
    // A parent chose "install updates right away" for this PC: a new build goes in as soon as it
    // arrives unless a game is running, instead of waiting for the next restart or sign-in.
    [property: JsonPropertyName("auto_update")] bool AutoUpdate = false,
    // Approved games the hub wants on this PC: the helper installs them by itself, no tap from the kid.
    List<InstallItem>? Installs = null,
    // The family's time zone (IANA, e.g. "America/New_York"). The hub decides "today" and bedtime in it, and the
    // helper sets the PC's clock to match so the time on his screen agrees with when the PC locks.
    string? Timezone = null);

/// <summary>One game on the PC's install list. Retry goes up each time a parent presses "Try again", and the helper starts that game over.</summary>
public sealed record InstallItem([property: JsonPropertyName("game_id")] int GameId, int Retry = 0);

/// <summary>Something the helper needs done in the kid's own session, where Steam and Battle.net run as him:
/// steam_start, steam_restart (so Steam reads a game it was just given), battlenet_install (Arg = product code).
/// The shell carries out each Id once.</summary>
public sealed record Nudge(string Id, string Kind, string Arg = "");

/// <summary>A one-off action a parent sent from the dashboard.</summary>
public sealed record HubCommand(int Id, string Kind, string Arg, [property: JsonPropertyName("created_by")] string CreatedBy);

/// <summary>A message from a parent, shown on the kid's screen.</summary>
public sealed record ParentMessage(int Id, string From, string Text, DateTimeOffset At);

public sealed record VoiceToken(string Url, string Room, string Token);

/// <summary>What the helper hands the shell. Installed and Locked are decided on the PC by the helper
/// (Locked includes the offline fallback), so the shell just displays them.</summary>
public sealed record ShellState(Rules? Rules, List<int> InstalledGameIds, bool Locked, bool HubReachable, DateTimeOffset FetchedAt,
    List<ParentMessage>? Messages = null,
    List<Nudge>? Nudges = null,
    Dictionary<int, string>? Installing = null,    // game id -> a few words for its tile while it installs by itself, e.g. "Downloading 45%"
    Dictionary<int, int>? InstallPercent = null,   // game id -> how far its download is, for the bar on its tile (only when known)
    List<string>? SignInNeeded = null);            // stores (steam, battlenet) waiting for a grown-up to sign in: their windows stay on screen

/// <summary>Reply to an install request: either the helper did it, or the shell should open a launcher URI.</summary>
public sealed record InstallResult(string Action, string? Uri, string Message, string? Args = null);

public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };
}

/// <summary>One chat message. Friend = an invited friend wrote it from the invite page. Picture = fetch it with
/// the "chat_picture_get" pipe op. Removed = a parent hid it (Text is then the hub's note saying so).</summary>
public sealed record ChatMessage(long Id, string Sender,
    [property: JsonPropertyName("kid_id")] string? KidId, string Text, string At, bool Mine,
    string Conv = "family", bool Friend = false, bool Picture = false, List<string>? Links = null, bool Removed = false,
    bool Video = false);   // a clip up to 30 s: Picture is its first frame; fetch the MP4 with "chat_video_get"

/// <summary>A conversation the kid can open: the family chat, his brothers, or one invited friend.</summary>
public sealed record ChatConv(string Id, string Title, string Kind,
    [property: JsonPropertyName("last_id")] long LastId,
    [property: JsonPropertyName("last_other_id")] long LastOtherId,
    [property: JsonPropertyName("can_send")] bool CanSend, string Note = "");

public sealed record ChatList(List<ChatConv> Convs, bool Pictures, bool Links, bool Videos = false);

public sealed record ChatPicture(string Data);

/// <summary>A match from the hub's kid-safe game search (Steam, Battle.net, PC Game Pass). Ref names the game and
/// the store it would come from, "steam:620" or "battlenet:WoWC", and goes with his request to Mom or Dad.</summary>
public sealed record GameHit(string Ref, string Name,
    [property: JsonPropertyName("image_url")] string ImageUrl, string? Esrb = null, string? Description = null, string? Store = null);
