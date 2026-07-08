# Pick Bot menu-bar widget (macOS)

Shows the Pick Bot in the MacBook menu bar without the website open:

```
⚡+2.34u ✓−1.10u
 └ live projected book   └ today's settled book
```

Dropdown: every live bet (score · win% · units), today's record, pending
count, and an "Open Pick Bot" link. Data comes from
`GET /api/handicapper/ticker` (shared-secret; 30s server cache — the
60-second poll costs effectively nothing on Vercel).

## Install (one time, ~2 minutes)

1. **Install SwiftBar** (free, open source; xbar also works):
   ```
   brew install swiftbar
   ```
   Launch it once and pick a plugin folder (default
   `~/Documents/SwiftBar` or similar).

2. **Store the secret** (the `FILLS_CRON_SECRET` value from Vercel, or set
   a dedicated `WIDGET_SECRET` env var there if you'd rather not reuse it):
   ```
   mkdir -p ~/.config/kahla
   echo 'KAHLA_TICKER_KEY=<paste secret here>' > ~/.config/kahla/widget.env
   chmod 600 ~/.config/kahla/widget.env
   ```

3. **Link the plugin** into SwiftBar's plugin folder:
   ```
   ln -s "/Users/robkahla/Documents/Kahla House/kahla-house/widget/pickbot.60s.py" \
         <your SwiftBar plugin folder>/pickbot.60s.py
   chmod +x "/Users/robkahla/Documents/Kahla House/kahla-house/widget/pickbot.60s.py"
   ```
   The `60s` in the filename IS the refresh interval — rename to
   `pickbot.30s.py` for faster updates (server cache is 30s, so going
   below that just re-reads the cache).

That's it. The widget updates every minute; click it for the per-bet
detail. If it shows `PB ⚙︎ setup`, the secret file is missing; `PB ⚠︎`
means the fetch failed (click for the error).

## Notes

- The feed is the **admin's book** (`_admin_uids` server-side). Another
  user's book: append `&uid=<their uid>` inside the script's URL — but
  the secret holder sees that book, so don't hand the secret out.
- `~/.config/kahla/widget.env` keeps the secret out of the repo. The
  script also honors `KAHLA_BASE_URL` there for local/dev testing.
