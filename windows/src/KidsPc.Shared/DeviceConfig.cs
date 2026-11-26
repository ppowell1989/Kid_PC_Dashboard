using System.Text.Json;

namespace KidsPc.Shared;

/// <summary>
/// C:\ProgramData\KidsPc\device.json, written by setup\Install-KidPc.ps1.
/// ACL: SYSTEM and Administrators only, so the kid's account cannot read the device token.
/// </summary>
public sealed class DeviceConfig
{
    public string HubUrl { get; set; } = "http://truenas.local:8080";   // GUESS: your TrueNAS hostname
    public string DeviceToken { get; set; } = "";
    public string KidUser { get; set; } = "";                           // Windows account name of the kid
    public string SteamPath { get; set; } = @"C:\Program Files (x86)\Steam";
    public string BattleNetPath { get; set; } = @"C:\Program Files (x86)\Battle.net\Battle.net.exe";
    public string BattleNetGamesRoot { get; set; } = @"C:\Program Files (x86)";
    public string EpicGamesRoot { get; set; } = @"C:\Program Files\Epic Games";   // where the Epic launcher puts games by default
    public string InstallerShare { get; set; } = "";                    // optional: \\truenas\kidspc-installers for "direct" games
    public int PollSeconds { get; set; } = 10;
    public int HistorySeconds { get; set; } = 120;                       // how often Chrome history is sent to the hub

    public static string Dir => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "KidsPc");
    public static string FilePath => Path.Combine(Dir, "device.json");

    public static DeviceConfig Load() =>
        JsonSerializer.Deserialize<DeviceConfig>(File.ReadAllText(FilePath), Json.Options)
        ?? throw new InvalidDataException("device.json is empty");
}
