# Launch checklist

What is done, and what still needs a person. Update it as you go.

## Done in code
- Listings, landing page and README only advertise commands that work on public servers.
- Data for a server (radio, panel, favorites, theme) is deleted when the bot is removed.
- Songs with non-commercial licences (CC "NC") are skipped, since there is a paid tier.
- Free-session limit only applies once `PREMIUM_SKU_ID` is set.
- Tests run on every pull request, and a push to `main` deploys only if they pass.
- Errors show users a generic message; details stay in the logs.
- The "we skip the commercials" claim is gone from the banner and public copy.

## Needs you before charging
- [ ] **Legal review.** Retransmitting radio streams in a paid product, the home server's commercial-station
      rotation with ad detection, and Jamendo's non-commercial free API key (get a Jamendo licence or leave
      `JAMENDO_CLIENT_ID` unset: ccMixter still works).
- [ ] **Privacy policy and terms** (`site/privacy.html`, `site/terms.html`) are drafts: fill the brackets
      (date, name, contact email) and have them reviewed.
- [ ] **Premium test.** Create the server subscription, set `PREMIUM_SKU_ID`, and test with a Discord test
      entitlement: Subscribe button, 24/7, theme, favorites, and what happens when it ends.
- [ ] **Landing page placeholders** in `site/index.html`: `CLIENT_ID`, `SUPPORT_INVITE`, `FORM_ENDPOINT`, price.
      Host `site/` (Cloudflare Pages: output directory `site`).
- [ ] **Capacity.** Public servers are capped (`MAX_PUBLIC_STREAMS`, default 3, +2 premium) on a shared 1 GB
      machine. Resize the Fly machine and raise the cap together before promoting the bot.
- [ ] **Monitoring.** One machine and one volume: add an uptime alert and a volume snapshot schedule.
- [ ] **Verification.** Discord requires bot verification at 100 servers; it asks for a privacy policy and terms.

- [ ] **Vote webhook.** Set `TOPGG_WEBHOOK_SECRET` and the webhook URL on top.gg, send a test delivery from the
      top.gg dashboard, vote once yourself and run `/vote`. After the deploy that adds the web server, check
      `fly status` and `https://<app>.fly.dev/healthz` (the app now has an `[http_service]`, with auto-stop off).

## Soft launch order
1. Your own support server and a few friends' servers.
2. Fix what they find; watch CPU and the "busy" message.
3. Then submit to top.gg, discord.bots.gg and discordbotlist.com (`docs/listings.md`).
