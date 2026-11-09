/**
 * Visit log for the brief page -- a Cloudflare Pages Function.
 *
 * Lives at functions/_middleware.js in the REPOSITORY ROOT (next to docs/, not inside it).
 * Cloudflare Pages picks the folder up on the next deployment; docs/_routes.json limits it
 * to the two HTML pages and /api/*, so every other file stays a plain static asset.
 *
 * On every page view it serves the page first, then -- after the response has gone out --
 * posts one row to the feedback script (the same Apps Script that takes 👍/👎), which
 * appends it to the "Visits" tab of the sheet: time, address, city, device, page. No
 * cookie, no script on the page, nothing the reader can see.
 *
 * Needs two variables on the Pages project (Settings -> Variables and secrets):
 *   FEEDBACK_URL  the feedback script's /exec URL (same value as the GitHub variable)
 *   VISIT_TOKEN   the visit token shown on admin.html (also a script property there)
 * Without them the function does nothing but pass the request through.
 */

export async function onRequest(context) {
  const { request, env, next, waitUntil } = context;
  const res = await next();
  try {
    if (request.method !== "GET" || !env.FEEDBACK_URL || !env.VISIT_TOKEN) return res;
    const url = new URL(request.url);
    const path = url.pathname;
    if (!(path === "/" || path.endsWith(".html"))) return res;
    if (res.status >= 400) return res;
    const ct = res.headers.get("content-type") || "";
    if (res.status !== 304 && !ct.includes("text/html")) return res;
    const purpose = request.headers.get("purpose") || request.headers.get("sec-purpose") || "";
    if (/prefetch|prerender/i.test(purpose)) return res;
    waitUntil(logVisit(request, env, path + url.search));
  } catch (e) {
    // never let the log break the page
  }
  return res;
}

async function logVisit(request, env, page) {
  const cf = request.cf || {};
  const ua = request.headers.get("user-agent") || "";
  const who = classify(ua);
  let ref = "";
  try {
    const r = request.headers.get("referer");
    if (r) {
      const u = new URL(r);
      ref = u.host === new URL(request.url).host ? "" : u.host;
    }
  } catch (e) {}
  const body = {
    type: "visit",
    token: env.VISIT_TOKEN,
    ip: request.headers.get("cf-connecting-ip") || "",
    country: cf.country || "",
    region: cf.region || cf.regionCode || "",
    city: cf.city || "",
    device: who.device,
    browser: who.browser,
    os: who.os,
    bot: who.bot,
    page,
    ref,
    ua: ua.slice(0, 160),
  };
  try {
    await fetch(env.FEEDBACK_URL, {
      method: "POST",
      headers: { "content-type": "text/plain;charset=UTF-8" },
      body: JSON.stringify(body),
      redirect: "follow",
    });
  } catch (e) {}
}

/** Device / browser / OS from the user-agent string; crawlers and link previews flagged. */
export function classify(ua) {
  const u = ua || "";
  if (/WhatsApp/i.test(u)) return { device: "Bot", browser: "WhatsApp preview", os: "", bot: true };
  if (/TelegramBot/i.test(u)) return { device: "Bot", browser: "Telegram preview", os: "", bot: true };
  if (/facebookexternalhit|Facebot|Twitterbot|LinkedInBot|Slackbot|Discordbot|Pinterest|Embedly/i.test(u))
    return { device: "Bot", browser: "Link preview", os: "", bot: true };
  if (!u || /bot|crawl|spider|slurp|headless|lighthouse|python-requests|python-httpx|curl\/|wget|go-http|java\/|libwww|monitor|uptime|scan/i.test(u))
    return { device: "Bot", browser: "Crawler", os: "", bot: true };
  const os = /Android/i.test(u) ? "Android"
    : /iPhone|iPad|iPod/i.test(u) ? "iOS"
    : /Windows/i.test(u) ? "Windows"
    : /Mac OS X|Macintosh/i.test(u) ? "macOS"
    : /CrOS/i.test(u) ? "ChromeOS"
    : /Linux/i.test(u) ? "Linux" : "";
  const device = /iPad|Tablet|(Android(?!.*Mobile))/i.test(u) ? "Tablet"
    : /Mobi|iPhone|Android/i.test(u) ? "Mobile" : "Desktop";
  const browser = /Telegram/i.test(u) ? "Telegram in-app"
    : /FBAN|FBAV|FB_IAB/i.test(u) ? "Facebook in-app"
    : /Instagram/i.test(u) ? "Instagram in-app"
    : /EdgA?\/|Edge\//i.test(u) ? "Edge"
    : /SamsungBrowser/i.test(u) ? "Samsung Internet"
    : /OPR\/|Opera/i.test(u) ? "Opera"
    : /Firefox|FxiOS/i.test(u) ? "Firefox"
    : /CriOS|Chrome/i.test(u) ? "Chrome"
    : /Safari/i.test(u) ? "Safari" : "Other";
  return { device, browser, os, bot: false };
}
