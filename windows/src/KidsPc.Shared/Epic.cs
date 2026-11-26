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
    private static readonly string Pf86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
    private static readonly string CommonData = Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);

    /// <summary>The launcher's own folder. Nothing in here is a game.</summary>
    public static string LauncherFolder => Path.Combine(Pf86, "Epic Games", "Launcher");

    private static string Bin(string arch) => Path.Combine(LauncherFolder, "Portal", "Binaries", arch, "EpicGamesLauncher.exe");

    /// <summary>EpicGamesLauncher.exe. Newer launchers ship a Win64 build next to the old Win32 one.</summary>
    public static string LauncherExe => File.Exists(Bin("Win64")) ? Bin("Win64") : File.Exists(Bin("Win32")) ? Bin("Win32") : Bin("Win64");

    public static bool LauncherInstalled => File.Exists(Bin("Win64")) || File.Exists(Bin("Win32"));

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
