# Test run: Windows VM on the MacBook, hub on TrueNAS

Goal: exercise the shell, helper, time limits, requests and Chrome reporting end to end before the
real PCs arrive. Games themselves won't run well in a VM (no real GPU), so test with a small, light
Steam game or two; the point is the controls, not frame rates.

## A. Hub on TrueNAS (about 20 minutes)

In the TrueNAS web UI: **System → Shell** (or SSH in), then:

```bash
cd /mnt/<pool>/apps                       # any dataset you like
git clone https://github.com/ppowell1989/kid_pc_dashboard.git kidspc   # private: use a GitHub token as the password
cd kidspc
mkdir -p /mnt/<pool>/apps/kidspc-data/hub
cp hub/config.example.yaml /mnt/<pool>/apps/kidspc-data/hub/config.yaml
sudo docker run --rm -v "$PWD/hub:/h" -w /h python:3.12-slim sh -c "pip -q install pyyaml pyjwt fastapi >/dev/null && python -m app.hashpw 'YOUR-PASSWORD'"
```

Edit `config.yaml`: paste the hash into both parent entries, replace every `CHANGE-ME` token with a
random string (`openssl rand -hex 24`), and for testing set Adam's `daily_minutes` to `{ weekday: 10, weekend: 10 }`
so the warning and lock happen quickly. Leave `adguard.enabled: false` and skip voice for the first pass.

Start just the hub:

```bash
cd deploy
sed -i "s#/mnt/tank/apps/kidspc#/mnt/<pool>/apps/kidspc-data#" docker-compose.yml
sudo docker compose up -d --build hub
```

Check `http://<truenas-ip>:8080` on your phone. Sign in, then on **Games** add a free Steam game for
Adam (e.g. a small free-to-play title; the Steam app id is the number in its store URL).

## B. Windows VM on the MacBook (about an hour, mostly downloads)

On an Apple Silicon Mac this is **Windows 11 on ARM**. x64 apps (Steam, Chrome) run under Windows'
built-in emulation; our shell and helper are built natively for ARM64.

1. Install **UTM** (free, mac.getutm.app) or VMware Fusion (free for personal use). Parallels also works.
2. Get the Windows 11 ARM64 ISO from Microsoft's download page and create a VM: 4 cores, 8 GB RAM, 64 GB disk.
3. During setup make a local admin account (e.g. `parent`). In UTM, install the SPICE guest tools afterwards.
4. Bridged networking, so the VM is on the home network and can reach `http://<truenas-ip>:8080`.
5. As the admin: install Chrome and Steam. Sign in to Steam with a test account.
6. Download the **kidspc-win-arm64** build from the repo's GitHub Actions page (Actions → latest "build" run → Artifacts) and unzip it to `C:\kidspc`.
7. Open PowerShell **as administrator**:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd C:\kidspc\setup
.\Install-KidPc.ps1 -KidUser adam -KidName Adam -HubUrl http://<truenas-ip>:8080 `
   -DeviceToken <adam device_token> -ExtensionToken <adam extension_token>
```

8. Sign in as `adam` once (a normal desktop appears this first time), sign out, back to `parent`, run the
   same command with `-ShellOnly` added. Skip Autologon for the VM; just sign in as adam.
9. Sign in as adam again: the kid shell should appear instead of the desktop.

**Escape hatch while testing:** Ctrl+Alt+Del → Sign out, then sign in as `parent`. To remove the shell
for adam: as parent, `reg load HKU\K C:\Users\adam\NTUSER.DAT`, delete
`HKU\K\Software\Microsoft\Windows NT\CurrentVersion\Winlogon\Shell`, `reg unload HKU\K`.
Take a VM snapshot before step 7 so you can roll back in seconds.

## C. Test script

Use the checklist at the end of `docs/SETUP.md`, plus:

- [ ] Dashboard shows Adam online and "Playing <game>" within a minute of launching it
- [ ] With a 10-minute limit: warning at 10 left, lock at 0, game closed within 30 s
- [ ] "Ask for 15 more minutes" → request on your phone → approve → shell unlocks within ~30 s
- [ ] "Get more games" lists approved-but-not-installed games; Install opens Steam to that game
- [ ] Starting a Steam game that is NOT approved closes it within 30 s
- [ ] Sign in as `parent`: nothing is enforced
- [ ] Stop the hub container: shell says it can't reach the server; time keeps counting down

Send me anything that breaks (a photo of the screen is fine). Logs: `Event Viewer → Windows Logs → Application`, source `KidsPcHelper`.
