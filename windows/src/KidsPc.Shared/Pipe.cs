using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace KidsPc.Shared;

/// <summary>
/// Shell &lt;-&gt; helper protocol: one JSON object per line over a local named pipe.
/// The shell never holds the hub's device token; it asks the helper, which runs as SYSTEM.
/// </summary>
public static class PipeProtocol
{
    public const string PipeName = "KidsPcHelper";
}

public sealed record PipeRequest(string Op, string? Kind = null, string? Detail = null, string? Note = null, int? GameId = null, string? Ref = null);

public sealed record PipeResponse(bool Ok, string? Error = null, JsonElement? Data = null);

public sealed class HelperClient
{
    public async Task<PipeResponse> SendAsync(PipeRequest req, CancellationToken ct = default)
    {
        await using var pipe = new NamedPipeClientStream(".", PipeProtocol.PipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        await pipe.ConnectAsync(3000, ct);
        using var reader = new StreamReader(pipe, Encoding.UTF8, leaveOpen: true);
        await using var writer = new StreamWriter(pipe, new UTF8Encoding(false), leaveOpen: true) { AutoFlush = true };
        await writer.WriteLineAsync(JsonSerializer.Serialize(req, Json.Options));
        var line = await reader.ReadLineAsync(ct) ?? throw new IOException("helper closed the pipe");
        return JsonSerializer.Deserialize<PipeResponse>(line, Json.Options)!;
    }

    public async Task<T?> GetAsync<T>(PipeRequest req, CancellationToken ct = default)
    {
        var res = await SendAsync(req, ct);
        if (!res.Ok) throw new InvalidOperationException(res.Error ?? "helper error");
        return res.Data is { } d ? d.Deserialize<T>(Json.Options) : default;
    }
}
