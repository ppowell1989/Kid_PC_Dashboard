# Buying games for the kids

No game store lets an outside app pay for a game. Steam, Battle.net, Epic and Microsoft only sell through their own
checkout, signed in as the buyer. So the hub handles everything around the payment and none of the payment itself.
It never sees card details, store passwords or receipts.

## The flow

1. **Buy** appears on a kid's game request ("Approve & buy"), on any approved game his account doesn't have
   (Family & Devices, Requests → Stuck installs, Games), and its steps can be reopened while a purchase is open.
2. Pressing it **approves the game** (so it's on his PC's install list) and **opens a purchase** in the hub: kid, game,
   store, the price the hub showed (Steam only), who pressed Buy. If it came from a request, the kid gets
   "Yes! … is approved. Patrick is getting it for you now."
3. The sheet shows the store's own page and the steps for getting it onto the right kid's account:
   - **Steam**: buy it on your own account in a Steam Family (shared straight away, nothing to accept; one copy plays
     on one PC at a time), or "This is a gift" to his account (he accepts it once).
   - **Battle.net**: Buy as a gift to his BattleTag, or buy signed in as him. World of Warcraft is game time, not a purchase.
   - **Epic**: Buy as a gift to his Epic account (under 13: parental consent first).
   - **PC Game Pass**: nothing to buy; the Microsoft account on his PC needs the subscription.
   - Free games (Steam free-to-play, Overwatch 2, Hearthstone, Fortnite, Rocket League…) open no purchase.
4. **The hub notices it arrived** and closes the purchase:
   - Steam: his account lists it (Steam Web API key under Connections). While a Steam purchase is open the hub reads his
     games every 90 seconds instead of every 20 minutes.
   - Epic: his PC reports it in his Epic library.
   - Any store: his PC reports it actually downloading (more than 0 bytes) or installed. This is how a Steam Family
     share or a Battle.net gift shows up.
   - Or a parent presses **I bought it** (the only way for Battle.net before it starts downloading).
   Closing tells the PC to try the install again at once, and approve = install takes over.
5. **Bought for the kids** (Games page) lists what was bought, for whom, the price, who pressed Buy and how it arrived.
   Activity shows "Bought for Adam: Moving Out ($19.99)" under Parents.

## Code

`hub/app/purchases.py` (store pages, steps, purchase rows, the sweep), table `purchases` in `db.py`, endpoints in
`main.py`: `GET /api/parent/buy/{kid}/{game}` (look only), `POST /api/parent/purchases`, `GET /api/parent/purchases`,
`POST /api/parent/purchases/{id}/bought`, `POST /api/parent/purchases/{id}/cancel`.

## Guesses to check

- Blizzard shop page paths (`BATTLENET_PAGES`); others open the shop's front page.
- Epic has no store link per game in the hub, so Buy opens Epic's store search for the title.
- Whether a Steam gift needs accepting on the kid's side (it did at the time of writing) and where it pops up behind the shell.

## Later, for a cloud hub

A purchase row holds only ids, a price string and names, so it moves to a cloud hub unchanged. A parent's own Steam
account could be linked too, so a Family purchase is seen the moment it's bought rather than when it starts downloading.
