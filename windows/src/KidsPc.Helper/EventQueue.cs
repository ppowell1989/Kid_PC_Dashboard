using System.Collections.Concurrent;
using System.Net.Http.Json;

namespace KidsPc.Helper;

/// <summary>Things worth telling the parents (game stopped, unapproved game, app installed). Sent with the next heartbeat.</summary>
public sealed class EventQueue
{
    private readonly ConcurrentQueue<object> _queue = new();

    public void Add(string kind, string detail) =>
        _queue.Enqueue(new { kind, detail, at = DateTimeOffset.UtcNow.ToString("o") });

    public async Task FlushAsync(HttpClient http, CancellationToken ct)
    {
        if (_queue.IsEmpty) return;
        var batch = new List<object>();
        while (batch.Count < 100 && _queue.TryDequeue(out var e)) batch.Add(e);
        var r = await http.PostAsJsonAsync("/api/device/events", batch, ct);
        if (!r.IsSuccessStatusCode) foreach (var e in batch) _queue.Enqueue(e);   // retry next time
    }
}
