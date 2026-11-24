# Game consoles and the daily time budget

Goal: one daily time budget per kid that covers the PC and any game console.
Researched November 2026. None of the three console makers offers a public API for outside apps;
everything below uses the private services behind each maker's own parent app.

| | Nintendo Switch / Switch 2 | Xbox | PlayStation |
|---|---|---|---|
| Read today's play time | Yes, per player and per game | Yes (Family Safety activity) | Only by watching online presence |
| See the current game | Per game, after the Switch syncs | Yes | Yes, when online |
| Set the daily limit remotely | Yes (0 to 360 min) | Yes (schedules) | No (PlayStation Family app only) |
| Stop play now | Yes: limit = minutes played, with "Suspend software" | Yes: block the Xbox until a time | No |
| Works offline | Yes, the Switch enforces its own limit | Yes, Xbox enforces Family Safety | n/a |
| Account needed | Nintendo account + Parental Controls app | Microsoft account + Family group | PSN account (NPSSO cookie) |
| Library | [pynintendoparental](https://pypi.org/project/pynintendoparental/), built into Home Assistant | [pyfamilysafety](https://pypi.org/project/pyfamilysafety/) | [psnawp](https://pypi.org/project/PSNAWP/) |
| Risk | Unofficial; Nintendo can change it. Low ban risk, widely used | Unofficial; Microsoft sign-in is fiddly | Unofficial; read only |

Network blocking (AdGuard or the router) only cuts online play. Offline games keep working, so it is a backup, not a limit.

## What the hub does today (Nintendo)

`hub/app/consoles.py`. A parent links the Nintendo account on the Playtimes tab (sign in, then paste the
"Select this person" link). Every 3 minutes the hub:

1. Reads each Switch's minutes today for each player and stores them in `console_usage`.
2. Adds the minutes of players matched to a kid to that kid's "used" time, so the PC shows less time left.
3. For a Switch set to follow a kid's time, sets the Switch's daily limit to minutes played on it today plus the
   kid's minutes left (or to minutes played when the kid is locked), and turns on "Suspend software".

Limits:
- The Switch reports to Nintendo about every 15 minutes while online, so console minutes can arrive late.
- The Switch limit is per console, not per player. On a shared Switch it follows one kid.
- While bonus time added in Nintendo's own app is running, Nintendo refuses limit changes; the hub waits.
- The hub replaces the Switch's daily limit for today. Bedtime alarms in Nintendo's app are untouched.
- Not yet tested against a real Switch: built against the library's code, with Nintendo faked in tests.

Turn the feature off completely with `consoles: {enabled: false}` in config.yaml.

## Xbox (Microsoft Family Safety)

`hub/app/xbox.py`. A parent links the family organizer's Microsoft account on the Playtimes tab (sign in, then
paste the address of the blank page, which holds `code=`). Every 3 minutes the hub:

1. Reads each family member's Xbox screen time today and games played.
2. Adds the minutes of members matched to a kid to that kid's "used" time.
3. For a member with "Lock his Xbox when his time is up": while the kid is locked on the hub, blocks that member on
   Xbox until his next play time (or 6 AM), and lifts the block when he may play again (extra time, new day).
   The hub never lifts a block a parent set in Microsoft's app.

Limits:
- The Xbox can't be handed "minutes left", so the lock comes at the next check after time runs out (up to ~3 min),
  plus any lag in Microsoft's reports.
- Microsoft rotates the refresh token on every use; the hub stores the newest one each time. If the hub's data is
  restored from an old backup, the stored token may be stale: connect again.
- Not yet tested against a real Xbox: built against the library's code, with Microsoft faked in tests.

## Next, if wanted

- PlayStation: count minutes from online presence; limits stay in the PlayStation Family app.
- Multi-family hosting: `console_links` is keyed by provider only; it needs a household id like the rest of the hub.
