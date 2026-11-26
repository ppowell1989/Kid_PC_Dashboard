namespace KidsPc.Shared;

/// <summary>
/// Where the Epic Games Launcher keeps itself and its records on a PC, used by both the helper (installs, what is
/// running) and the shell (starting games). Games are named by their Epic "app name" (Fortnite, Sugar for Rocket
/// League), which is what the launcher's own links and its install records use.
///
/// GUESS where marked: these come from how the launcher has behaved for years (and what tools like Playnite and
/// Legendary read), not from anything Epic documents.
/// </summary>
public static class Epic
{
    private static readonly string Pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
    private static readonly string Pf86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
    private static readonly string CommonData = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);

    /// <summary>Where the launcher may live. Today's installer puts it in C:\Program Files\Epic Games\Launcher
    /// (seen on CI, launcher 1.3.210); older ones used Program Files (x86). Nothing in these folders is a game.</summary>
    public static readonly string[] LauncherFolders = [Path.Combine(Pf, "Epic Games", "Launcher"), Path.Combine(Pf86, "Epic Games", "Launcher")];

    /// <summary>Folders beside the games that Epic's installer fills with its own tools (never a game).</summary>
    public static readonly string[] ToolFolders = ["Launcher", "DirectXRedist", "Epic Online Services"];

    private static IEnumerable<string> Candidates() =>
        LauncherFolders.SelectMany(f => new[] { "Win64", "Win32" }.Select(arch => Path.Combine(f, "Portal", "Binaries", arch, "EpicGamesLauncher.exe")));

    /// <summary>EpicGamesLauncher.exe, wherever this PC's installer put it.</summary>
    public static string LauncherExe => Candidates().FirstOrDefault(File.Exists) ?? Candidates().First();

    public static bool LauncherInstalled => Candidates().Any(File.Exists);

    /// <summary>A path inside the launcher's own folders.</summary>
    public static bool IsLauncherPath(string path) =>
        LauncherFolders.Any(f => path.StartsWith(f + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase));

    /// <summary>C:\ProgramData\Epic\EpicGamesLauncher\Data: install records and the account's library cache.</summary>
    public static string DataDir => Path.Combine(CommonData, "Epic", "EpicGamesLauncher", "Data");

    /// <summary>One .item file (JSON) per installed or installing game: AppName, DisplayName, InstallLocation, InstallSize,
    /// bIsIncompleteInstall. GUESS: an install that has only just started may sit in Manifests\Pending.</summary>
    public static string Manifests => Path.Combine(DataDir, "Manifests");

    /// <summary>Base64 of a JSON list of catalogue items: the games on the signed-in account (GUESS, as Playnite reads it).</summary>
    public static string CatalogCache => Path.Combine(DataDir, "Catalog", "catcache.bin");

    /// <summary>The launcher's list of finished installs: {"InstallationList":[{"InstallLocation","AppName",...}]}.</summary>
    public static string LauncherInstalledDat => Path.Combine(CommonData, "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat");

    private static string App(string appName) => Uri.EscapeDataString(appName);

    /// <summary>Starts a game through the launcher, which stays in the background (silent=true).</summary>
    public static string LaunchUri(string appName) => $"com.epicgames.launcher://apps/{App(appName)}?action=launch&silent=true";

    /// <summary>Opens the launcher's Install box for a game on the account. GUESS: someone at the PC presses Install there.</summary>
    public static string InstallUri(string appName) => $"com.epicgames.launcher://apps/{App(appName)}?action=install";

    /// <summary>App names are letters, digits, and the odd dash or underscore (some are 32-character ids).</summary>
    public static bool IsAppName(string s) => s.Length is > 0 and <= 64 && s.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_');
}
