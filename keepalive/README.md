# Keepalive Worker

A Cloudflare Worker that requests `/healthz` on the booking API every ten
minutes, so the free Render service never sleeps.

Render spins a free service down after about fifteen minutes with no traffic,
and the next request then waits roughly fifty seconds while it starts again —
long enough for a caller to time out. This keeps it warm.

It replaces `.github/workflows/keepalive.yml`, which cannot run while the
GitHub account is locked for billing.

## Deploying

From this directory:

```bash
npx wrangler login     # opens a browser, authorises your Cloudflare account
npx wrangler deploy
```

`wrangler deploy` prints the Worker's URL and registers the schedule.

## Checking it

Open the Worker's URL in a browser, or:

```bash
curl https://clinic-keepalive.<your-subdomain>.workers.dev
```

That runs the same check the schedule runs and reports what came back:

```json
{
  "target": "https://clinic-booking-api-8iih.onrender.com/healthz",
  "result": { "status": 200, "ok": true, "ms": 412 }
}
```

To watch the scheduled runs:

```bash
npx wrangler tail
```

## Changing the target

Edit `TARGET_URL` in `wrangler.toml` and redeploy. No code change needed.

## Free plan limits

Five Cron Triggers per account and 100,000 requests a day. This uses one
trigger and about 4,300 requests a month, so it stays well inside the free
plan. A ten-minute schedule is `*/10 * * * *` in `wrangler.toml`.

The retry logic matters more than it looks: Cron Triggers get a 15 minute
wall-clock budget, so the Worker can wait out a cold start and report whether
the service really came back, instead of giving up after a few seconds the way
a short-timeout pinger would.
