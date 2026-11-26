# Epic Games on the kids' PCs

Epic Games is the fourth store next to Steam, Battle.net and PC Game Pass. It works the same way:
a parent approves a game on the hub, and the kid's PC puts it on by itself.

## What the PC does by itself

- **Installs the Epic Games app** the first time a kid has an approved Epic game. Epic ships a normal
  Windows installer package, so the helper installs it silently as SYSTEM (no one at the PC).
- **Lets the kid add game folders** in `C:\Program Files\Epic Games` (never the launcher's own folder there), and write the launcher's
  records in `C:\ProgramData\Epic`, so the Epic app, which runs as his standard account, can install games
  without asking for an admin password.
- **Starts the install.** Epic has no way to be told "install this game" from outside. The PC opens the
  Epic app's own Install box for the game on the kid's screen (`com.epicgames.launcher://apps/<app>?action=install`),
  and his tile says "Press Install in Epic". One press starts the download. After that the PCs page shows
  progress from the launcher's install record (`C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item`).
- **Easy Anti-Cheat.** Fortnite, Fall Guys and Rocket League use it, and its service needs an admin. Once a
  game is installed the helper runs the game's own `EasyAntiCheat_EOS_Setup.exe install <id>` as SYSTEM.
- **Launches** a game from its tile through the Epic app in the background (`?action=launch&silent=true`).
- **Closes unapproved Epic games.** Anything running from an Epic game's folder that a parent hasn't
  approved is closed, like an unapproved Steam game, and shows in Activity as a blocked game.
- **Hides Epic windows the PC opened by itself**, like Steam's, except its sign-in and Install box.
- **Tells the hub which games are on his Epic account** (read from the launcher's library cache), so the
  hub's game search can offer them. Search also knows Fortnite, Rocket League and Fall Guys without that.
- **Remove from PC** deletes the game's folder and the launcher's record of it.

## What a parent does once

1. **Sign in on each PC.** On the kid's screen, the Epic app asks for a sign-in the first time. Use the kid's
   own Epic account and tick *Remember me*. The PCs page says "Needs Epic sign-in" until this is done.
2. **Epic parental controls** (the kids are under 13, so Epic made their accounts *cabined accounts*).
   Until a parent consents from the email Epic sent ("Your Child Created an Epic Games Account"), a cabined
   account cannot chat, cannot spend real money and **cannot get new games that aren't Epic's own**. It can
   still play Fortnite, Rocket League and Fall Guys. At epicgames.com, Account, Parental Controls:
   - set the 6-digit PIN;
   - Epic Games Store rating: ESRB, capped at the same rating as the hub's ideas cap (E10+ by default);
   - Epic Games Payment: require the PIN for every purchase;
   - Friends: require the PIN to add or accept friends;
   - voice and text chat: *Friends only* or *Nobody*;
   - turn on the weekly playtime report if you want Epic's own email too.

## What can't be locked down from the PC

The Epic app's store and its free-games page can't be hidden from inside the app. The hub's controls are:
the PC closes any Epic game a parent hasn't approved, the kid has no tile that opens the Epic app on its own
(it comes up only when he starts an approved Epic game or the PC opens an Install box), and Epic's own parental
controls cap the store's ratings and require the PIN to buy.

## Guesses to check on a real PC

Marked `GUESS` in the code. None of these are documented by Epic:
- the launcher's install record fields, and whether a just-started download sits in `Manifests\Pending`;
- the sign-in check (`%LOCALAPPDATA%\EpicGamesLauncher\Saved\Config\Windows\GameUserSettings.ini`, `[RememberMe]`);
- the library cache format (`C:\ProgramData\Epic\EpicGamesLauncher\Data\Catalog\catcache.bin`);
- Fall Guys' app name (`0a2d9f6403244d12969e11da6713137b`);
- whether `?action=install` opens the Install box for a game the account doesn't have yet;
- the Easy Anti-Cheat setup arguments.

The test VM has no GPU, so a real game launch can only be checked on the Christmas PCs.
