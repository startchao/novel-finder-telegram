// worker.js — Cloudflare Workers HTTP proxy for novel sites
// Deploy at: Workers & Pages → Create Worker → paste this code
//
// Usage: GET https://novel-proxy.YOUR-SUBDOMAIN.workers.dev/?url=TARGET_URL
//   Optional params: method=POST, body=URLENCODED_FORM_DATA, referer=URL, cookie=VALUE

const ALLOWED_DOMAINS = [
  "69shuba.cx",
  "69shuba.com",
  "ptwxz.com",
  "uukanshu.com",
  "xbiquge.la",
  "xbiquge.so",
  "xbiquge.bid",
  "czbooks.net",
  "23us.so",
];

function isAllowed(url) {
  try {
    const hostname = new URL(url).hostname.replace(/^www\./, "");
    return ALLOWED_DOMAINS.some(
      (d) => hostname === d || hostname.endsWith("." + d)
    );
  } catch {
    return false;
  }
}

export default {
  async fetch(request) {
    const reqUrl = new URL(request.url);
    const targetUrl = reqUrl.searchParams.get("url");

    if (!targetUrl) {
      return new Response("Missing ?url= parameter", { status: 400 });
    }

    if (!isAllowed(targetUrl)) {
      return new Response("Domain not in whitelist", { status: 403 });
    }

    const method = (reqUrl.searchParams.get("method") || "GET").toUpperCase();
    const bodyParam = reqUrl.searchParams.get("body");

    const forwardHeaders = new Headers({
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
      Accept:
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Encoding": "gzip, deflate, br",
      "Upgrade-Insecure-Requests": "1",
      Connection: "keep-alive",
    });

    const referer = reqUrl.searchParams.get("referer");
    if (referer) forwardHeaders.set("Referer", referer);

    const cookie = reqUrl.searchParams.get("cookie");
    if (cookie) forwardHeaders.set("Cookie", cookie);

    const fetchInit = {
      method,
      headers: forwardHeaders,
      redirect: "follow",
    };

    if (method === "POST" && bodyParam) {
      forwardHeaders.set("Content-Type", "application/x-www-form-urlencoded");
      fetchInit.body = bodyParam;
    }

    try {
      const response = await fetch(targetUrl, fetchInit);

      const respHeaders = new Headers();
      const ct = response.headers.get("content-type");
      if (ct) respHeaders.set("content-type", ct);
      respHeaders.set("Access-Control-Allow-Origin", "*");

      const body = await response.arrayBuffer();
      return new Response(body, {
        status: response.status,
        headers: respHeaders,
      });
    } catch (err) {
      return new Response(`Proxy fetch error: ${err.message}`, { status: 502 });
    }
  },
};
