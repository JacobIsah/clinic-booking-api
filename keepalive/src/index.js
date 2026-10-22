/**
 * Keeps the clinic booking API awake.
 *
 * A free Render service sleeps after about 15 minutes with no traffic, and the
 * next request then waits roughly 50 seconds for it to start again - long
 * enough for a caller to give up first. This Worker requests /healthz every ten
 * minutes so the service never goes to sleep in the first place.
 *
 * Cron Triggers get a 15 minute wall-clock budget, which is why this can afford
 * to wait out a cold start and report whether the service actually came back,
 * rather than timing out partway and leaving it ambiguous.
 */

const DEFAULT_TARGET = "https://clinic-booking-api-8iih.onrender.com/healthz";
const ATTEMPTS = 3;
const RETRY_DELAY_MS = 20_000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function pingOnce(target) {
  // A cache-busting parameter, because Render sits behind a CDN and a cached
  // 200 would look like success while waking nothing. The API ignores unknown
  // query parameters on /healthz.
  const url = `${target}${target.includes("?") ? "&" : "?"}_ka=${Date.now()}`;
  const startedAt = Date.now();

  const response = await fetch(url, {
    headers: {
      "User-Agent": "clinic-keepalive/1.0",
      "Cache-Control": "no-cache",
    },
    cf: { cacheTtl: 0, cacheEverything: false },
  });

  return { status: response.status, ok: response.ok, ms: Date.now() - startedAt };
}

async function keepAwake(env) {
  const target = env.TARGET_URL || DEFAULT_TARGET;
  let last = null;

  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    try {
      const result = await pingOnce(target);
      last = result;
      if (result.ok) {
        console.log(`keepalive ok: ${target} -> ${result.status} in ${result.ms}ms (attempt ${attempt})`);
        return result;
      }
      console.warn(`keepalive got ${result.status} from ${target} (attempt ${attempt})`);
    } catch (error) {
      last = { status: 0, ok: false, error: String(error) };
      console.warn(`keepalive error on attempt ${attempt}: ${error}`);
    }

    // A waking service can refuse a connection briefly before it starts
    // answering, so give it room rather than calling it dead on one failure.
    if (attempt < ATTEMPTS) await sleep(RETRY_DELAY_MS);
  }

  console.error(`keepalive failed after ${ATTEMPTS} attempts: ${JSON.stringify(last)}`);
  return last;
}

export default {
  // Runs on the schedule in wrangler.toml.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(keepAwake(env));
  },

  // Visiting the Worker's own URL runs the same check, so you can confirm it
  // works without waiting for the next tick.
  async fetch(request, env) {
    const result = await keepAwake(env);
    return new Response(JSON.stringify({ target: env.TARGET_URL || DEFAULT_TARGET, result }, null, 2), {
      status: result && result.ok ? 200 : 502,
      headers: { "Content-Type": "application/json" },
    });
  },
};
