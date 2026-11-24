# Windows side: kid shell + admin helper

.NET 8. GUESS: WPF for the shell (mature, simple to make full-screen, WebView2 for the voice window)
and a .NET Worker Service for the helper.

| Project | Runs as | Job |
|---|---|---|
| `KidsPc.Shell` | the kid (standard user), as his Windows shell instead of Explorer | Home screen, game tiles, "get more games", time left, 10-minute warning, lock screen, ask for time/games, Chrome tile, voice window |
| `KidsPc.Helper` | Windows service, LocalSystem | Holds the hub token. Heartbeats + pulls rules every 10 s. Stops games when time is up or a game isn't approved. Rewrites Chrome's URL lists. Answers the shell over a named pipe. |
| `KidsPc.Shared` | | Hub contracts, pipe protocol, device config |

The shell never talks to the hub directly, so the kid's account never sees the device token
(`C:\ProgramData\KidsPc\device.json` is SYSTEM/Administrators only).

## How the controls hold

- **No desktop.** Per-user `Winlogon\Shell` points at `KidsPcShell.exe`, so there is no Start menu,
  taskbar or File Explorer. Task Manager, Run, regedit, cmd and Control Panel are off by policy.
- **Time limits are enforced by the helper, not the shell.** At zero it kills game and Chrome processes
  every 10 s. The shell's lock screen is just the friendly face of that.
- **Unapproved games get stopped.** The helper maps running processes to Steam app ids via
  `appmanifest_*.acf` and to Battle.net games by folder, and kills anything not approved for that kid.
  Steam's own store is still reachable from the Steam client, so also turn on **Steam Families**
  (store and library restrictions) for each kid's Steam account.
- **Offline.** If the hub is unreachable for 15+ minutes, the helper keeps counting down from the last
  known time and locks at zero. If it has never reached the hub since boot, it stays locked. (GUESS: fail closed.)
- **Parents are left alone.** Enforcement only applies when the kid's account is the one at the console.

## Installing games (what the helper actually does)

Steam and Battle.net install games for a standard user without admin rights, so for those the
helper's job is gatekeeping: the shell asks the helper, the helper checks the game is approved, and
only then does the shell open `steam://install/<appid>` or Battle.net. "Direct" games (anything else)
are installed by the helper as SYSTEM from an installer share (`\\truenas\kidspc-installers\<gameId>\setup.exe`).

## Build and install

```powershell
.\build.ps1                       # on any Windows PC with the .NET 8 SDK
.\setup\Install-KidPc.ps1 -KidUser adam -KidName Adam -HubUrl http://truenas.local:8080 `
    -DeviceToken <from config.yaml> -ExtensionToken <from config.yaml>
```

This code has **not been compiled yet** (the build machine had no .NET SDK). Expect a round of small
compile fixes on first build.

## Known gaps (TODO)

- Game Pass: installed detection and the Microsoft account sign-in are not wired (`game_pass_enabled` is off by default).
- Battle.net: game folder table in `GameLibrary.cs` covers a few titles; extend as you approve more.
- App control: PowerShell and downloaded .exe files are not yet blocked for the kid (AppLocker/WDAC rules).
- Kill events are logged on the PC but not yet reported to the hub dashboard.
- Game art on tiles (catalogue has an `image_url` field; tiles show titles only).
