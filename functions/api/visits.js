/**
 * GET /api/visits?t=<visit token>&limit=100 -> the visit totals for admin.html, fetched
 * from the feedback script (which keeps the "Visits" tab) and passed straight through.
 * The token is checked here against VISIT_TOKEN and again by the script, so the log is
 * never readable without the admin PIN the token derives from.
 */

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const t = url.searchParams.get("t") || "";
  const limit = Math.min(500, parseInt(url.searchParams.get("limit") || "100", 10) || 100);
  const headers = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
  if (!env.FEEDBACK_URL || !env.VISIT_TOKEN) {
    return new Response(JSON.stringify({ error: "not configured: set FEEDBACK_URL and VISIT_TOKEN on the site host" }), { status: 503, headers });
  }
  if (!t || t !== env.VISIT_TOKEN) {
    return new Response(JSON.stringify({ error: "denied" }), { status: 403, headers });
  }
  try {
    const r = await fetch(env.FEEDBACK_URL + "?type=visits&token=" + encodeURIComponent(t) + "&limit=" + limit, { redirect: "follow" });
    const text = await r.text();
    let ok = r.ok;
    try { JSON.parse(text); } catch (e) { ok = false; }
    return new Response(ok ? text : JSON.stringify({ error: "the feedback script answered " + r.status + " -- is the new version deployed?" }),
      { status: ok ? 200 : 502, headers });
  } catch (e) {
    return new Response(JSON.stringify({ error: "could not reach the feedback script" }), { status: 502, headers });
  }
}
