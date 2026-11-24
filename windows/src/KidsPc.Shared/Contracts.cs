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
    [property: JsonPropertyName("lock_reason")] string LockReason,
    bool Warn);

public sealed record Game(
    int Id, string Title, string Launcher,
    [property: JsonPropertyName("launch_ref")] string LaunchRef,
    string Rating,
    [property: JsonPropertyName("image_url")] string ImageUrl);

public sealed record WebRules(string Mode, [property: JsonPropertyName("allowed_sites")] List<string> AllowedSites);

public sealed record PendingRequest(int Id, string Kind, string Detail, string State,
    [property: JsonPropertyName("created_at")] string CreatedAt);

public sealed record Friend(string Code, [property: JsonPropertyName("friend_name")] string FriendName);

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
    List<HubCommand>? Commands = null);

/// <summary>A one-off action a parent sent from the dashboard.</summary>
public sealed record HubCommand(int Id, string Kind, string Arg, [property: JsonPropertyName("created_by")] string CreatedBy);

/// <summary>A message from a parent, shown on the kid's screen.</summary>
public sealed record ParentMessage(int Id, string From, string Text, DateTimeOffset At);

public sealed record VoiceToken(string Url, string Room, string Token);

/// <summary>What the helper hands the shell. Installed and Locked are decided on the PC by the helper
/// (Locked includes the offline fallback), so the shell just displays them.</summary>
public sealed record ShellState(Rules? Rules, List<int> InstalledGameIds, bool Locked, bool HubReachable, DateTimeOffset FetchedAt,
    List<ParentMessage>? Messages = null);

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

public sealed record ChatMessage(long Id, string Sender,
    [property: JsonPropertyName("kid_id")] string? KidId, string Text, string At, bool Mine);
