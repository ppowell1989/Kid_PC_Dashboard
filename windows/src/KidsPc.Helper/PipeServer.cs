using System.Diagnostics;
using System.IO.Pipes;
using System.Net.Http.Json;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using KidsPc.Shared;

namespace KidsPc.Helper;

/// <summary>
/// Local named pipe the shell talks to. Every request is checked against the rules the helper
/// pulled from the hub itself, so a kid who finds the pipe still cannot install unapproved games.
/// </summary>
public sealed class PipeServer(HttpClient http, DeviceConfig cfg, StateCache cache, GameLibrary library, AppInstaller apps, EventQueue events, GameDeployer deployer, ILogger<PipeServer> log)
    : BackgroundService
{
    // What goes wrong inside the helper is for the log and the parents, never for the kid's screen.
    private const string NotNow = "That didn't work just now. Try again in a minute.";
    private readonly Dictionary<string, DateTimeOffset> _told = [];     // when the parents were last told about each problem

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            var pipe = CreatePipe();
            try
            {
                await pipe.WaitForConnectionAsync(ct);
                _ = Task.Run(() => HandleAsync(pipe, ct), ct);
            }
            catch (OperationCanceledException) { await pipe.DisposeAsync(); break; }
        }
    }

    private static NamedPipeServerStream CreatePipe()
    {
        var sec = new PipeSecurity();
        // The creating account needs full control to create further pipe instances (SYSTEM when run as the service).
        sec.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User!, PipeAccessRights.FullControl, AccessControlType.Allow));
        sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null), PipeAccessRights.FullControl, AccessControlType.Allow));
        sec.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.AuthenticatedUserSid, null), PipeAccessRights.ReadWrite, AccessControlType.Allow));
        return NamedPipeServerStreamAcl.Create(PipeProtocol.PipeName, PipeDirection.InOut, NamedPipeServerStream.MaxAllowedServerInstances,
            PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 4096, 4096, sec);
    }

    private async Task HandleAsync(NamedPipeServerStream pipe, CancellationToken ct)
    {
        await using var _ = pipe;
        using var reader = new StreamReader(pipe, Encoding.UTF8, leaveOpen: true);
        await using var writer = new StreamWriter(pipe, new UTF8Encoding(false), leaveOpen: true) { AutoFlush = true };
        PipeResponse res;
        try
        {
            var line = await reader.ReadLineAsync(ct);
            var req = JsonSerializer.Deserialize<PipeRequest>(line ?? "{}", Json.Options)!;
            res = await DispatchAsync(req, ct);
        }
        catch (Exception ex)
        {
            log.LogWarning("pipe request failed: {Msg}", ex.Message);
            res = new PipeResponse(false, NotNow);
        }
        await writer.WriteLineAsync(JsonSerializer.Serialize(res, Json.Options));
        // Without draining, closing the server end can discard the reply before the client reads it.
        try { pipe.WaitForPipeDrain(); } catch (IOException) { /* client already gone */ }
    }

    private static PipeResponse Ok(object data) => new(true, null, JsonSerializer.SerializeToElement(data, Json.Options));

    private async Task<PipeResponse> DispatchAsync(PipeRequest req, CancellationToken ct)
    {
        switch (req.Op)
        {
            case "state":
                // A game still being put on the PC is not ready to play, even if its folder is already there.
                var installing = deployer.Captions();
                return Ok(cache.Snapshot(library.InstalledGameIds(cache.Rules).Where(id => !installing.ContainsKey(id)), deployer.Nudges(), installing,
                    deployer.Percents(), deployer.SignInNeeded()));

            case "request":   // ask a parent for a game, more time, or a website
                if (req.Kind is not ("game" or "time" or "site") || string.IsNullOrWhiteSpace(req.Detail))
                    return new(false, "bad request");
                var r = await http.PostAsJsonAsync("/api/device/requests", new { kind = req.Kind, detail = req.Detail, note = req.Note ?? "", @ref = req.Ref ?? "" }, ct);
                return r.IsSuccessStatusCode ? Ok(new { sent = true }) : new(false, "The hub did not accept the request.");

            case "voice_token":
                if (cache.Snapshot([]).Locked) return new(false, "Voice chat is off while the PC is locked.");
                var vt = await http.GetFromJsonAsync<VoiceToken>("/api/device/voice-token", Json.Options, ct);
                return Ok(vt!);

            case "family_voice_token":   // the brothers' shared room; no invite link needed
                if (cache.Snapshot([]).Locked) return new(false, "Voice chat is off while the PC is locked.");
                var fr = await http.GetAsync("/api/device/family-voice-token", ct);
                if (!fr.IsSuccessStatusCode) return new(false, fr.StatusCode == System.Net.HttpStatusCode.Forbidden ? "Family voice is turned off right now." : "The home server didn't answer.");
                return Ok((await fr.Content.ReadFromJsonAsync<VoiceToken>(Json.Options, ct))!);

            case "voice_state":   // the shell opened or closed a voice window
                cache.Voice = req.Detail is "family" or "friends" ? req.Detail : "";
                return Ok(new { });

            case "game_search":   // "Get more games": Steam, Battle.net and Game Pass; the hub filters out adult-only titles
                if (string.IsNullOrWhiteSpace(req.Detail)) return Ok(Array.Empty<GameHit>());
                var found = await http.GetFromJsonAsync<List<GameHit>>($"/api/device/games/search?q={Uri.EscapeDataString(req.Detail)}", Json.Options, ct);
                return Ok(found ?? []);

            case "game_discover":   // "Get more games" shelves to browse; the hub applies the parent's rating cap
                return Ok(await http.GetFromJsonAsync<Discover>("/api/device/games/discover", Json.Options, ct) ?? new Discover("", []));

            // Chat: the family chat (works while locked, so he can always reach Mom and Dad), his brothers' chat and one
            // per invited friend. The hub decides who may talk to whom and when; its "no" comes back as a plain sentence.
            case "chat_convs":
                return await HubJson<ChatList>(await http.GetAsync("/api/device/chats", ct), ct);

            case "chat":      // messages in conversation Ref (default family) after id Detail
                var after = long.TryParse(req.Detail, out var a) ? a : 0;
                var conv = Uri.EscapeDataString(req.Ref ?? "family");
                return await HubJson<List<ChatMessage>>(await http.GetAsync($"/api/device/chat?after={after}&conv={conv}", ct), ct);

            case "chat_send":
                if (string.IsNullOrWhiteSpace(req.Detail) || req.Detail.Length > 500) return new(false, "Message is empty or too long.");
                return await HubJson<object>(await http.PostAsJsonAsync("/api/device/chat", new { text = req.Detail, conv = req.Ref ?? "family" }, ct), ct);

            case "chat_picture":   // Detail = the picture as base64 (the shell has already made it small), Note = words with it
                if (string.IsNullOrEmpty(req.Detail) || req.Detail.Length > 21_000_000) return new(false, "That picture is too big. Try a smaller one.");
                return await HubJson<object>(await http.PostAsJsonAsync("/api/device/chat/picture",
                    new { data = req.Detail, text = req.Note ?? "", conv = req.Ref ?? "family" }, ct), ct);

            case "chat_video":   // Detail = the clip as base64 (at most 100 MB); the hub converts it, which can take a minute
                if (string.IsNullOrEmpty(req.Detail) || req.Detail.Length > 140_000_000) return new(false, "That video is too big. Try a shorter one.");
                byte[] clip;
                try { clip = Convert.FromBase64String(req.Detail); } catch (FormatException) { return new(false, NotNow); }
                using (var body = new ByteArrayContent(clip))
                {
                    body.Headers.ContentType = new("application/octet-stream");
                    var url = $"/api/device/chat/video?conv={Uri.EscapeDataString(req.Ref ?? "family")}&text={Uri.EscapeDataString(req.Note ?? "")}";
                    return await HubJson<object>(await Slow.PostAsync(url, body, ct), ct);
                }

            case "chat_video_get":   // Detail = message id; the hub answers with a signed link to the clip, which this follows
                var vr = await Slow.GetAsync($"/api/device/chat/video/{(long.TryParse(req.Detail, out var vid) ? vid : 0)}", ct);
                if (!vr.IsSuccessStatusCode) return new(false, "That video isn't there any more.");
                return Ok(new ChatPicture(Convert.ToBase64String(await vr.Content.ReadAsByteArrayAsync(ct))));

            case "chat_picture_get":   // Detail = message id
                var pr = await http.GetAsync($"/api/device/chat/picture/{(long.TryParse(req.Detail, out var pid) ? pid : 0)}", ct);
                if (!pr.IsSuccessStatusCode) return new(false, "That picture isn't there any more.");
                return Ok(new ChatPicture(Convert.ToBase64String(await pr.Content.ReadAsByteArrayAsync(ct))));

            case "install":
                return Ok(await InstallAsync(req.GameId ?? -1, ct));

            case "problem":   // the shell could not start something: the parents hear about it, the kid got a plain sentence
                if (!string.IsNullOrWhiteSpace(req.Detail)) TellParents("launch_failed", req.Detail.Length > 300 ? req.Detail[..300] : req.Detail);
                return Ok(new { });

            default:
                return new(false, $"unknown op {req.Op}");
        }
    }

    private HttpClient? _slow;
    /// <summary>The hub, for chat videos: same address and device token, but minutes instead of 10 seconds.</summary>
    private HttpClient Slow => _slow ??= new HttpClient { BaseAddress = http.BaseAddress, Timeout = TimeSpan.FromMinutes(5),
        DefaultRequestHeaders = { Authorization = http.DefaultRequestHeaders.Authorization } };

    /// <summary>The hub's answer as data, or its "no" as the sentence it wrote for the kid
    /// ("This chat is off while the PC is locked."). A server error is not for his eyes.</summary>
    private static async Task<PipeResponse> HubJson<T>(HttpResponseMessage r, CancellationToken ct)
    {
        using var _ = r;
        if (r.IsSuccessStatusCode)
            return Ok((await r.Content.ReadFromJsonAsync<T>(Json.Options, ct))!);
        if ((int)r.StatusCode is 403 or 404 or 413 or 422 or 429)
        {
            try
            {
                using var doc = JsonDocument.Parse(await r.Content.ReadAsStringAsync(ct));
                if (doc.RootElement.TryGetProperty("detail", out var d) && d.ValueKind == JsonValueKind.String)
                    return new(false, d.GetString());
            }
            catch (JsonException) { }
        }
        return new(false, NotNow);
    }

    private async Task<InstallResult> InstallAsync(int gameId, CancellationToken ct)
    {
        var game = cache.Rules?.Games.FirstOrDefault(g => g.Id == gameId);
        if (game is null) return new("refused", null, "That game is not approved.");

        switch (game.Launcher)
        {
            // Steam and Battle.net install games for a standard user without elevation.
            // The shell opens the launcher at the right page; the helper only gates which games.
            case "steam":
                if (LauncherMissing("steam", "Steam", game) is { } noSteam) return noSteam;
                return new("uri", $"steam://install/{game.LaunchRef}", "Steam will download it.");
            case "battlenet":
                if (LauncherMissing("battlenet", "Battle.net", game) is { } noBattleNet) return noBattleNet;
                return new("exec", cfg.BattleNetPath, "Battle.net will download it.", $"--exec=\"install {game.LaunchRef}\"");   // GUESS: Battle.net CLI verb
            case "gamepass":
                return new("uri", $"ms-windows-store://pdp/?ProductId={game.LaunchRef}", "The Xbox app will download it.");
            case "direct":
                // Admin install from the parents' installer share: <share>\<gameId>\setup.exe, run silently as SYSTEM.
                var setup = Path.Combine(cfg.InstallerShare, gameId.ToString(), "setup.exe");
                if (string.IsNullOrEmpty(cfg.InstallerShare) || !File.Exists(setup))
                    return new("refused", null, "No installer found for this game. Ask a parent.");
                var args = File.Exists(setup + ".args") ? await File.ReadAllTextAsync(setup + ".args", ct) : "/S";
                using (var p = Process.Start(new ProcessStartInfo(setup, args) { UseShellExecute = false, CreateNoWindow = true })!)
                    await p.WaitForExitAsync(ct);
                return new("done", null, $"{game.Title} is installed.");
            default:
                return new("refused", null, "Unknown launcher.");
        }
    }

    /// <summary>The program a game installs through (Steam, Battle.net) is not on this PC. Instead of handing the
    /// shell a program that is not there, start installing it, tell the parents, and ask the kid to come back.</summary>
    private InstallResult? LauncherMissing(string app, string name, Game game)
    {
        var st = apps.Want(app);
        if (st.State == "installed") return null;
        TellParents("launcher_missing", $"{name} is not on this PC; {cache.Rules?.Kid.Name ?? "he"} wanted {game.Title}. "
            + (st.State == "failed" ? "It would not install by itself." : "Installing it now."));
        return new("wait", null, st.State == "failed"
            ? $"{name} needs a parent to set it up on this PC. They've been told."
            : $"{name} is being set up on this PC first. Try {game.Title} again in a few minutes.");
    }

    /// <summary>Puts a line in the parents' activity feed, at most once every ten minutes per problem.</summary>
    private void TellParents(string kind, string detail)
    {
        lock (_told)
        {
            var key = kind + "|" + detail;
            if (_told.TryGetValue(key, out var at) && DateTimeOffset.Now - at < TimeSpan.FromMinutes(10)) return;
            if (_told.Count > 200) _told.Clear();
            _told[key] = DateTimeOffset.Now;
        }
        events.Add(kind, detail);
    }
}
