"""Game art audit: does every game the hub can show have a picture that loads and is the right game?

Run on CI (it needs the internet): python -m hub.tests.art_audit OUT_DIR
Writes OUT_DIR/report.md, OUT_DIR/report.json and contact sheets (sheet-*.png) to look at by eye.
Exits 1 when a curated game (idea shelves, Battle.net, Epic) has no art, or its art doesn't load.
Game Pass art comes from Microsoft's catalogue for hundreds of games: broken ones are listed, and fail the run
only when more than a few are broken.

--discover also looks for candidate art for curated games that have none (official pages' og:image and the
Microsoft Store catalogue) and draws those on their own sheet, so a person can pick the right picture.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from hub.app import discover, gamesearch
from hub.app.main import STEAM_ART

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}

# Official pages to read og:image from, for --discover.
PAGES = {
    "battlenet:WoW": ["https://worldofwarcraft.blizzard.com/en-us/"],
    "battlenet:WoWF": ["https://worldofwarcraft.blizzard.com/en-us/"],
    "battlenet:WoWC": ["https://worldofwarcraft.blizzard.com/en-us/wowclassic", "https://classic.warcraft.com/"],
    "battlenet:WoWCE": ["https://worldofwarcraft.blizzard.com/en-us/wowclassic/hardcore", "https://classic.warcraft.com/"],
    "battlenet:WoWA": ["https://worldofwarcraft.blizzard.com/en-us/wowclassic", "https://classic.warcraft.com/"],
    "battlenet:WTCG": ["https://hearthstone.blizzard.com/en-us"],
    "battlenet:Hero": ["https://heroesofthestorm.blizzard.com/en-us/"],
    "battlenet:S2": ["https://starcraft2.blizzard.com/en-us/"],
    "battlenet:S1": ["https://starcraft.blizzard.com/en-us/"],
    "battlenet:W3": ["https://warcraft3.blizzard.com/en-us/"],
    "battlenet:D3": ["https://diablo3.blizzard.com/en-us/"],
    "battlenet:OSI": ["https://diablo2.blizzard.com/en-us/"],
    "battlenet:ANBS": ["https://diabloimmortal.blizzard.com/en-us/"],
    "battlenet:RTRO": ["https://www.blizzard.com/en-us/games/arcade-collection", "https://blizzardarcadecollection.blizzard.com/en-us/",
                       "https://us.shop.battle.net/en-us/product/blizzard-arcade-collection", "https://news.blizzard.com/en-us/article/23606232/blizzard-arcade-collection-now-available"],
    "battlenet:Pro": ["https://overwatch.blizzard.com/en-us/"],
    "battlenet:Fen": ["https://diablo4.blizzard.com/en-us/"],
    "epic:Fortnite": ["https://www.fortnite.com/", "https://store.epicgames.com/en-US/p/fortnite"],
    "epic:Sugar": ["https://www.rocketleague.com/en"],
    "epic:0a2d9f6403244d12969e11da6713137b": ["https://www.fallguys.com/en-US"],
}


# Curated games with no art we can link to: they show the plain tile, and that's expected.
NO_ART_OK = {"battlenet:RTRO"}


def curated() -> list[dict]:
    """Every curated game, with the art URL the hub hands out today (gamesearch.art_for)."""
    out = []
    for i in discover.all_ideas():
        out.append({"key": f"{i['launcher']}:{i['launch_ref']}", "title": i["title"], "where": f"idea shelf: {i['shelf']}",
                    "launcher": i["launcher"], "ref": i["launch_ref"]})
    for b in gamesearch.BATTLENET:
        out.append({"key": f"battlenet:{b['code']}", "title": b["title"], "where": "Battle.net list", "launcher": "battlenet",
                    "ref": b["code"], "steam": b.get("steam")})
    for e in gamesearch.EPIC:
        out.append({"key": f"epic:{e['app']}", "title": e["title"], "where": "Epic list", "launcher": "epic",
                    "ref": e["app"], "steam": e.get("steam")})
    once: dict[str, dict] = {}
    for g in out:   # once each, where it first appears, keeping the Steam twin from the store lists
        k = g["key"].lower()
        if k in once:
            once[k]["steam"] = once[k].get("steam") or g.get("steam")
        else:
            once[k] = g
    out = list(once.values())
    for g in out:
        g["url"] = gamesearch.art_for(g["launcher"], g["ref"])
    return out


async def fetch_image(c: httpx.AsyncClient, url: str) -> tuple[str, Image.Image | None]:
    """'' and the picture if it loads as an image, else why not."""
    if not url:
        return "no art", None
    try:
        r = await c.get(url, headers=UA, follow_redirects=True)
    except Exception as e:
        return f"error {type(e).__name__}", None
    if r.status_code != 200:
        return f"HTTP {r.status_code}", None
    if not r.headers.get("content-type", "").startswith("image/"):
        return f"not an image ({r.headers.get('content-type', '?')})", None
    try:
        im = Image.open(io.BytesIO(r.content))
        im.load()
    except Exception:
        return "won't decode", None
    if im.format not in ("JPEG", "PNG"):
        return f"{im.format} picture (the Windows tiles need JPEG or PNG)", im.convert("RGB")
    if im.width < 200 or im.height < 90:
        return f"too small ({im.width}x{im.height})", im
    return "", im.convert("RGB")


async def steam_name(c: httpx.AsyncClient, appid: str) -> str:
    try:
        r = await c.get("https://store.steampowered.com/api/appdetails", params={"appids": appid, "l": "english", "cc": "US"})
        d = r.json().get(appid) or {}
        return d["data"]["name"] if d.get("success") else "(Steam has no such app)"
    except Exception as e:
        return f"(Steam lookup failed: {type(e).__name__})"


async def og_images(c: httpx.AsyncClient, page: str) -> list[str]:
    try:
        r = await c.get(page, headers=UA, follow_redirects=True)
    except Exception:
        return []
    found = re.findall(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::url)?["\'][^>]*content=["\']([^"\']+)', r.text)
    found += re.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']', r.text)
    return list(dict.fromkeys(u.replace("&amp;", "&") for u in found if u.startswith("http")))


async def ms_store_images(c: httpx.AsyncClient, title: str) -> list[str]:
    """Art from the Microsoft Store catalogue for a game sold there (many Blizzard games are)."""
    try:
        r = await c.get("https://displaycatalog.mp.microsoft.com/v7.0/productFamilies/autosuggest",
                        params={"market": "US", "languages": "en-us", "query": title, "productFamilyNames": "Games"})
        ids = [p.get("ProductId") for fam in r.json().get("Results") or [] for p in fam.get("Products") or []][:3]
        if not ids:
            return []
        r = await c.get("https://displaycatalog.mp.microsoft.com/v7.0/products",
                        params={"bigIds": ",".join(ids), "market": "US", "languages": "en-us"})
        out = []
        for p in r.json().get("Products") or []:
            x = gamesearch.xbox_product(p)
            if x and x["image_url"] and gamesearch.norm(title).split()[0] in gamesearch.norm(x["title"]):
                out.append(x["image_url"])
        return out
    except Exception:
        return []


def sheet(rows: list[tuple[str, str, Image.Image | None]], path: Path, cols: int = 4) -> None:
    """A contact sheet: each picture as the tile shows it (cropped to 262x150) with its label under it."""
    w, h, lab = 300, 170, 46
    n = max(1, len(rows))
    out = Image.new("RGB", (cols * w, ((n + cols - 1) // cols) * (h + lab)), "white")
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    for k, (label, problem, im) in enumerate(rows):
        x, y = (k % cols) * w, (k // cols) * (h + lab)
        if im is not None:
            t = im.copy()
            scale = max((w - 10) / t.width, (h - 10) / t.height)
            t = t.resize((max(1, int(t.width * scale)), max(1, int(t.height * scale))))
            left, top = (t.width - (w - 10)) // 2, (t.height - (h - 10)) // 2
            out.paste(t.crop((left, top, left + w - 10, top + h - 10)), (x + 5, y + 5))
        else:
            d.rectangle((x + 5, y + 5, x + w - 5, y + h - 5), fill="#f3d6d6")
        d.text((x + 6, y + h), label[:46], fill="black", font=font)
        d.text((x + 6, y + h + 16), (problem or "ok")[:46], fill="#b00000" if problem else "#007000", font=font)
    out.save(path)


async def main(out: Path, discover_mode: bool) -> int:
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"curated": [], "gamepass": {}, "candidates": []}
    lines = ["# Game art audit", ""]
    failed = False
    async with httpx.AsyncClient(timeout=20, limits=httpx.Limits(max_connections=12)) as c:
        games = curated()
        results = await asyncio.gather(*(fetch_image(c, g["url"]) for g in games))
        names = await asyncio.gather(*(steam_name(c, g["ref"] if g["launcher"] == "steam" else g.get("steam") or "")
                                       if g["launcher"] == "steam" or g.get("steam") else asyncio.sleep(0, "") for g in games))
        rows = []
        lines += ["## Curated games", "", "| Game | Where | Art | Steam says |", "|---|---|---|---|"]
        for g, (problem, im), sname in zip(games, results, names):
            if sname and gamesearch.norm(sname).split()[:1] != gamesearch.norm(g["title"]).split()[:1]:
                problem = problem or f"Steam name is '{sname}'"
            failed |= bool(problem) and g["key"] not in NO_ART_OK
            report["curated"].append({**g, "problem": problem, "steam_name": sname, "size": im.size if im else None})
            rows.append((f"{g['title']} [{g['key'].split(':')[0]}]", problem, im))
            if im is not None:   # a copy, for screenshots made where these sites can't be reached
                (out / "img").mkdir(exist_ok=True)
                t = im.convert("RGB")
                t.thumbnail((920, 520))
                t.save(out / "img" / (re.sub(r"[^A-Za-z0-9]+", "_", g["key"]) + ".jpg"), quality=88)
            lines.append(f"| {g['title']} | {g['where']} | {problem or 'ok'} | {sname} |")
        sheet(rows, out / "sheet-curated.png")

        # Game Pass: the whole PC catalogue, as the hub reads it.
        try:
            gp = await gamesearch._fetch_gamepass(c)
        except Exception as e:
            gp = []
            report["gamepass"]["error"] = repr(e)
        gp_results = await asyncio.gather(*(fetch_image(c, g["image_url"]) for g in gp))
        broken = [{"title": g["title"], "url": g["image_url"], "problem": p} for g, (p, _) in zip(gp, gp_results) if p]
        report["gamepass"].update(count=len(gp), broken=broken)
        lines += ["", "## PC Game Pass", "", f"{len(gp)} games in the catalogue, {len(broken)} without working art.", ""]
        lines += [f"- {b['title']}: {b['problem']}" for b in broken]
        sample = [(g["title"], p, im) for g, (p, im) in list(zip(gp, gp_results))[:24]]
        sheet(sample, out / "sheet-gamepass.png")
        if not gp or len(broken) > max(3, len(gp) // 50):
            failed = True

        if discover_mode:
            cand_rows = []
            for g in games:
                if g["launcher"] == "steam" or (g["url"] and g["key"] not in NO_ART_OK):
                    continue   # only look for art for games that have none
                urls = []
                for page in PAGES.get(g["key"], []):
                    urls += await og_images(c, page)
                urls += await ms_store_images(c, g["title"].split(" (")[0])
                for u in list(dict.fromkeys(urls))[:4]:
                    p, im = await fetch_image(c, u)
                    report["candidates"].append({"key": g["key"], "title": g["title"], "url": u, "problem": p,
                                                 "size": im.size if im else None})
                    cand_rows.append((f"{g['title'][:30]} #{len([r for r in report['candidates'] if r['key'] == g['key']])}", p, im))
            sheet(cand_rows, out / "sheet-candidates.png", cols=4)
            lines += ["", "## Candidates", ""] + [f"- {r['key']} #{i}: {r['url']} {r['problem'] or ''} {r['size']}"
                                                   for i, r in enumerate(report["candidates"])]

    (out / "report.json").write_text(json.dumps(report, indent=1))
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(asyncio.run(main(Path(args[0] if args else "art-audit"), "--discover" in sys.argv)))
