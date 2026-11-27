"""Buying a game for a kid from the parent dashboard.

No game store lets an outside app pay for a game: Steam, Battle.net, Epic and Microsoft only sell through their own
checkout, signed in as the buyer. So the hub does everything around the payment and none of the payment itself:

  1. A parent presses Buy on a kid's request, on an approved game his account doesn't have, or in Games.
  2. The hub approves the game (so it's on his PC's install list) and opens a purchase: which kid, which game,
     which store, and the price it showed. It sends back the store's own page and a few plain steps for getting
     the game onto the right kid's account (a gift, or Steam Family sharing).
  3. The parent pays on the store's site or app, on their own phone.
  4. The hub notices the game arrived and the purchase closes by itself:
        Steam       the kid's Steam account lists it (steamlink.py reads it with the Steam Web API key), or
        Epic        the kid's PC reports it in his Epic library, or
        any store   his PC reports it installed (this is how a Steam Family share or a Battle.net gift shows up).
     A parent can also press "I bought it" (the only way for Battle.net while it's downloading).
     Either way the PC is told to try the install again straight away, and approve = install takes over.

The hub never sees card details, store passwords or receipts. It keeps the kid, the game, the store, the price
it showed, who pressed Buy and when it arrived. That is all a later cloud hub would hold too: the money stays
between the parent and the store.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote_plus

from .db import DB

STATES = ("started", "bought", "cancelled")

STORE_NAME = {"steam": "Steam", "battlenet": "Battle.net", "epic": "Epic Games", "gamepass": "PC Game Pass"}

# Battle.net games that cost nothing to play, so there's nothing to buy.
BATTLENET_FREE = {"pro", "wtcg", "hero", "s2", "anbs"}
# Battle.net games played with a subscription (game time) rather than bought once.
BATTLENET_SUBSCRIPTION = {"wow", "wowf", "wowc", "wowce", "wowa"}
# Blizzard shop pages. GUESS: the "family" page names follow shop.battle.net's own links for these games; any other
# game opens the shop's front page, which has its own search.
BATTLENET_SHOP = "https://shop.battle.net/en-us"
BATTLENET_PAGES = {
    "wow": "family/world-of-warcraft", "wowf": "family/world-of-warcraft", "wowc": "family/world-of-warcraft-classic",
    "wowce": "family/world-of-warcraft-classic", "wowa": "family/world-of-warcraft-classic",
    "fen": "family/diablo-iv", "osi": "family/diablo-ii-resurrected", "d3": "family/diablo-iii",
    "w3": "family/warcraft-iii-reforged", "s1": "family/starcraft-remastered", "wlby": "family/crash-bandicoot-4",
    "rtro": "family/blizzard-arcade-collection",
}
# Epic's own games in the hub's list are free to play.
EPIC_FREE = {"fortnite", "sugar", "0a2d9f6403244d12969e11da6713137b"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_url(launcher: str, ref: str, title: str) -> str:
    """The store's own page for this game, where the parent pays."""
    if launcher == "steam":
        return f"https://store.steampowered.com/app/{ref}/"
    if launcher == "battlenet":
        page = BATTLENET_PAGES.get(ref.lower())
        return f"{BATTLENET_SHOP}/{page}" if page else BATTLENET_SHOP
    if launcher == "epic":
        return f"https://store.epicgames.com/en-US/browse?q={quote_plus(title)}&sortBy=relevancy&sortDir=DESC"
    if launcher == "gamepass":
        return f"https://www.xbox.com/en-US/games/store/x/{ref}"
    return ""


def guide(game: dict, kid_name: str, *, owned: bool | None = None, free: bool = False, price: str = "",
          steam_account: dict | None = None) -> dict:
    """How to buy this game for this kid, in a parent's words.

    Returns store, url, price, kind (buy, free, subscription, included, owned, none), title (for the sheet's heading),
    steps (short sentences, in order) and detects (how the hub will notice it arrived, one sentence)."""
    launcher, ref, title = game["launcher"], game["launch_ref"], game["title"]
    store = STORE_NAME.get(launcher, "")
    out = {"store": store, "url": store_url(launcher, ref, title), "price": price, "kind": "buy",
           "title": f"Buy {title} for {kid_name}", "steps": [], "detects": ""}
    if launcher == "direct" or not store:
        return {**out, "kind": "none", "title": f"{title} isn't from a store",
                "steps": ["It installs from the home server, so there's nothing to buy here."]}
    if owned:
        return {**out, "kind": "owned", "title": f"{kid_name} already has {title}",
                "steps": [f"It's on {kid_name}'s {store} account already, so there's nothing to buy. It installs by itself."]}

    if launcher == "steam":
        who = (steam_account or {}).get("name") or (steam_account or {}).get("login") or ""
        acct = f"{kid_name}'s Steam account" + (f" ({who})" if who else "")
        out["detects"] = (f"The hub checks {acct} every couple of minutes and starts the install as soon as the game is there."
                          if steam_account else f"The hub sees it once Steam is signed in on {kid_name}'s PC and the game starts installing.")
        if free:
            return {**out, "kind": "free", "title": f"{title} is free",
                    "steps": [f"Nothing to pay. {kid_name}'s PC opens Steam's Install box by itself: press Install there once.",
                              f"Or open the store page signed in as {kid_name} and press Play Game or Add to Account."]}
        out["steps"] = [
            "Open the Steam page below in the Steam app or a browser, signed in as you.",
            f"Best if you're in a Steam Family with {kid_name}: buy it for your own account. Steam shares it with him straight "
            "away and there's nothing for him to accept. One copy plays on one PC at a time, so two kids playing it "
            "together need a copy each.",
            f"Or buy it as a gift: Add to Cart, then choose \"This is a gift\" and pick {acct}. He has to be on your Steam friends "
            "list, and he accepts the gift once (it pops up in Steam on his PC, or you can accept it signed in as him).",
        ]
        return out

    if launcher == "battlenet":
        code = ref.lower()
        out["detects"] = f"Battle.net doesn't let the hub look at {kid_name}'s account, so press \"I bought it\" when you're done. Installing starts then."
        if code in BATTLENET_FREE:
            return {**out, "kind": "free", "title": f"{title} is free to play",
                    "steps": ["Nothing to buy. It installs on his PC by itself once Battle.net is signed in there."]}
        if code in BATTLENET_SUBSCRIPTION:
            out["kind"] = "subscription"
            out["steps"] = [f"World of Warcraft is played with game time (a monthly subscription) on {kid_name}'s Battle.net account, "
                            "not bought once.",
                            f"Sign in to the Blizzard shop as {kid_name} (or gift game time to his BattleTag from your account) and add game time.",
                            "Classic and Forever versions use the same game time."]
            return out
        out["steps"] = ["Open the Blizzard shop below, signed in as you.",
                        f"Pick {title}, then choose Buy as a gift and send it to {kid_name}'s BattleTag. He has to be on your Battle.net "
                        "friends list.",
                        f"Or sign in to the shop as {kid_name} and buy it on his account directly."]
        return out

    if launcher == "epic":
        out["detects"] = f"{kid_name}'s PC reports his Epic library, so the hub sees it there and starts the install."
        if ref.lower() in EPIC_FREE:
            return {**out, "kind": "free", "title": f"{title} is free",
                    "steps": [f"Nothing to pay. {kid_name}'s PC opens Epic's Install box by itself: press Install there once."]}
        out["steps"] = ["Open the Epic store below, signed in as you, and find the game.",
                        f"Choose Buy as a gift and pick {kid_name}'s Epic account. He has to be on your Epic friends list.",
                        f"If {kid_name} is under 13, his Epic account only takes games from other publishers after you've given "
                        "consent in Epic's parental controls (epicgames.com, Account, Parental Controls)."]
        return out

    # Game Pass: a subscription, not a purchase.
    return {**out, "kind": "included", "title": f"{title} comes with PC Game Pass",
            "steps": ["Games in PC Game Pass aren't bought one by one: they come with the subscription.",
                      f"The Microsoft account signed in to the Xbox app on {kid_name}'s PC needs PC Game Pass. "
                      "Add it with Microsoft Family (family.microsoft.com) or at xbox.com."]}


# --------------------------------------------------------------------------- purchase rows

def open_purchase(db: DB, kid_id: str, game_id: int) -> dict | None:
    return db.one("SELECT * FROM purchases WHERE kid_id=? AND game_id=? AND state='started' ORDER BY id DESC LIMIT 1", (kid_id, game_id))


def start(db: DB, kid_id: str, game_id: int, store: str, price: str, parent: str, request_id: int | None = None) -> dict:
    """Open a purchase (or return the one already open for this kid and game)."""
    have = open_purchase(db, kid_id, game_id)
    if have:
        return have
    pid = db.x("INSERT INTO purchases(kid_id,game_id,store,price,started_by,started_at,request_id) VALUES(?,?,?,?,?,?,?)",
               (kid_id, game_id, store, price[:30], parent, now_iso(), request_id))
    return db.one("SELECT * FROM purchases WHERE id=?", (pid,))


def close(db: DB, pid: int, how: str, by: str = "") -> bool:
    """Mark a purchase bought. how: steam, epic, pc (it installed) or parent. Returns False if it wasn't open."""
    p = db.one("SELECT * FROM purchases WHERE id=?", (pid,))
    if not p or p["state"] != "started":
        return False
    db.x("UPDATE purchases SET state='bought', bought_at=?, confirmed=?, confirmed_by=? WHERE id=?", (now_iso(), how, by, pid))
    # Tell the PC to try the install again now, rather than at its next round.
    db.x("""UPDATE game_installs SET retry=retry+1, state='queued', detail='', done_bytes=0, total_bytes=0
            WHERE kid_id=? AND game_id=? AND state NOT IN ('installed','installing') AND remove=0""", (p["kid_id"], p["game_id"]))
    g = db.one("SELECT title FROM games WHERE id=?", (p["game_id"],))
    db.x("INSERT INTO events(kid_id,kind,detail,at) VALUES(?,?,?,?)",
         (p["kid_id"], "game_bought", (g or {}).get("title", "a game") + (f" ({p['price']})" if p["price"] else ""),
          datetime.now().isoformat(timespec="seconds")))
    return True


def cancel(db: DB, pid: int) -> bool:
    p = db.one("SELECT state FROM purchases WHERE id=?", (pid,))
    if not p or p["state"] != "started":
        return False
    db.x("UPDATE purchases SET state='cancelled' WHERE id=?", (pid,))
    return True


def sweep(db: DB, steam_owned, epic_apps) -> list[int]:
    """Close every open purchase whose game has arrived. steam_owned(kid_id) -> set of app ids or None;
    epic_apps(kid_id) -> set of lower-case Epic app names. Returns the ids it closed."""
    closed = []
    rows = db.q("""SELECT p.id, p.kid_id, p.game_id, g.launcher, g.launch_ref, i.state AS install, i.done_bytes
                   FROM purchases p JOIN games g ON g.id=p.game_id
                   LEFT JOIN game_installs i ON i.kid_id=p.kid_id AND i.game_id=p.game_id
                   WHERE p.state='started'""")
    for r in rows:
        how = ""
        # Installed, or really downloading (Steam can sit at "installing" with nothing coming for a game it has no licence for).
        if r["install"] == "installed" or (r["install"] == "installing" and (r["done_bytes"] or 0) > 0):
            how = "pc"
        elif r["launcher"] == "steam" and r["launch_ref"] in (steam_owned(r["kid_id"]) or set()):
            how = "steam"
        elif r["launcher"] == "epic" and r["launch_ref"].lower() in epic_apps(r["kid_id"]):
            how = "epic"
        if how and close(db, r["id"], how):
            closed.append(r["id"])
    return closed


HOW_WORDS = {"steam": "Steam shows it on his account", "epic": "it's in his Epic library", "pc": "it started installing on his PC",
             "parent": "marked bought"}


def describe(p: dict) -> str:
    """One line for the purchases list."""
    if p["state"] == "started":
        return "Waiting for it to arrive on his account. Installing starts by itself then."
    if p["state"] == "cancelled":
        return "Not bought."
    how = HOW_WORDS.get(p.get("confirmed") or "", "")
    who = f" by {p['confirmed_by']}" if p.get("confirmed") == "parent" and p.get("confirmed_by") else ""
    return f"Arrived: {how}{who}." if how else "Arrived."
