#!/usr/bin/env bash
# waf_probe.sh — measure how often transfermarkt.com answers with an AWS WAF
# captcha instead of the page, FROM WHEREVER YOU RUN IT.
#
# Why this ships in the repo rather than living in a notebook: the README and
# .env.example both quote a figure (8 of 10 requests challenged from a bare
# datacentre address, 2026-09-16), and a number describing a living thing has
# to arrive with the command that reproduces it or not be written down at
# all. That figure is a property of an address on a day, not of the site —
# from a proxied exit the same day, the real page came back every time.
#
#   ./tools/waf_probe.sh                 # 10 requests to market-values
#   ./tools/waf_probe.sh 20              # 20 requests
#   ./tools/waf_probe.sh 10 transfers    # a different mode
#
# Modes: market-values | club-squad | transfers | player
#
# Reading the output: a page the site actually served answers from
# `server: nginx`, its own origin. A refusal answers from `server: CloudFront`
# with `x-amzn-waf-action: captcha` — CloudFront generated it at the edge and
# the request never reached Transfermarkt at all.

set -u

N="${1:-10}"
MODE="${2:-market-values}"

case "$MODE" in
  market-values) URL="https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop" ;;
  club-squad)    URL="https://www.transfermarkt.com/x/startseite/verein/281" ;;
  transfers)     URL="https://www.transfermarkt.com/statistik/neuestetransfers" ;;
  player)        URL="https://www.transfermarkt.com/x/profil/spieler/418560" ;;
  *) echo "unknown mode: $MODE (expected market-values|club-squad|transfers|player)"; exit 2 ;;
esac

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
HDR="$(mktemp)"; BODY="$(mktemp)"
trap 'rm -f "$HDR" "$BODY"' EXIT

echo "URL:  $URL"
echo "when: $(date -u '+%Y-%m-%d %H:%M:%SZ')   requests: $N"
echo

challenged=0
for i in $(seq 1 "$N"); do
  # -L matters: the player and club URLs answer 301 to their canonical slug,
  # and without it every redirect would be counted as a clean response.
  code=$(curl -sSL -o "$BODY" -D "$HDR" -w "%{http_code}" "$URL" \
           -H "User-Agent: $UA" --max-time 40 2>/dev/null)
  srv=$(grep -i '^server:' "$HDR" | tr -d '\r' | tail -1 | awk '{print $2}')
  waf=$(grep -i '^x-amzn-waf-action:' "$HDR" | tr -d '\r' | tail -1 | awk '{print $2}')
  title=$(grep -o -i '<title>[^<]*</title>' "$BODY" | head -1 \
            | sed 's/<[^>]*>//g' | cut -c1-46)
  [ -n "${waf:-}" ] && challenged=$((challenged + 1))
  printf "%3d  http=%-4s server=%-11s waf=%-8s %s\n" \
    "$i" "$code" "${srv:-?}" "${waf:-none}" "$title"
  sleep 2
done

echo
echo "challenged: $challenged of $N"
echo
if [ "$challenged" -gt 0 ]; then
  echo "This exit meets the WAF. A proxy or the Scraping Browser API is what"
  echo "gets the page from here; TWOCAPTCHA_KEY is what solves the challenge"
  echo "when one still appears (task type AmazonTaskProxyless)."
else
  echo "This exit was not challenged in $N attempts. That is a fact about this"
  echo "address today, not about the site: on 2026-09-16 the same URL from one"
  echo "datacentre address returned 200 and 405 interleaved. Run it again, and"
  echo "run at least 10 before concluding anything."
fi
