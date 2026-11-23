# Chrome lockdown and reporting

Two pieces:

- `policy/Set-ChromePolicy.ps1` writes the static machine policies (incognito off, guest mode off,
  SafeSearch and YouTube Restricted forced, dev tools off, history can't be deleted, DNS-over-HTTPS off
  so AdGuard sees every lookup, all extensions blocked except ours). `Install-KidPc.ps1` calls it.
- `extension/` is a small Manifest V3 extension that reports every top-level page (URL, title, time)
  to the hub's `/api/device/visits`, once a minute, queueing while the hub is offline. Its hub URL and
  token come from Chrome policy (`chrome.storage.managed`), so the kid can't see or change them in the UI.

The allow-list (`web_mode: allowlist`) is not set here. The admin helper rewrites `URLBlocklist` and
`URLAllowlist` whenever the hub's rules change (see `windows/src/KidsPc.Helper/ChromePolicy.cs`).

## Getting the extension onto the PCs (needs a decision)

Chrome on Windows only force-installs extensions from the Chrome Web Store, unless the machine is
domain-joined or enrolled in Chrome Enterprise Core. Two workable routes:

1. **Unlisted Chrome Web Store item.** Upload `extension/` as an *unlisted* item (one-time $5 developer
   fee, a Google account, review takes a few days). Only people with the link can find it. Pass its id
   to `Install-KidPc.ps1 -ExtensionId <id>`. Recommended. Allow about a week before Christmas for review.
2. **Chrome Enterprise Core** (free browser management, Google Admin console). Lets you host the
   `.crx` yourself, but ties the browsers to a Google admin account, which cuts against local-first.

Until one of these is done, everything else works; only page-level history is missing
(AdGuard still shows which sites were visited).

## Testing the extension by hand

`chrome://extensions` → Developer mode → Load unpacked → `extension/`. Managed storage is empty when
loaded this way, so for a local test set the policy keys under
`HKLM\SOFTWARE\Policies\Google\Chrome\3rdparty\extensions\<unpacked id>\policy` (`hubUrl`, `token`).
