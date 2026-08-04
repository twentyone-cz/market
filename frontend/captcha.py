"""Compute Captcha — ochrana anonymních endpointů před boty.

Widget v prohlížeči spočítá výpočetní challenge a vrátí solve_token; ten se
tady ověřuje server-to-server (POST /captcha/v1/verify se site_secret).
Konfigurace je runtime (admin UI → settings v DB), env jen jako výchozí
hodnota; bez site_key + site_secret je captcha prostě vypnutá.

Fail-open: když je služba nedostupná (timeout, 5xx), požadavek propustíme —
pod captchou stále leží rate-limit a samotná identita je kryptografická
(LNURL-auth). Neplatný/použitý token se ale odmítá vždy.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_API = "https://captcha.qr6.eu"
PRESET = "strict"  # ~10 s, doporučeno pro login/platby
TIMEOUT = 8


def _setting(store, key, env, fallback=""):
    return store.get_setting(key) or os.environ.get(env, fallback)


def config(store):
    """Vrací (api_url, site_key, site_secret) — prázdné = captcha vypnutá."""
    return (
        (_setting(store, "captcha_api", "CAPTCHA_API", DEFAULT_API) or DEFAULT_API).rstrip("/"),
        _setting(store, "captcha_site_key", "CAPTCHA_SITE_KEY"),
        _setting(store, "captcha_site_secret", "CAPTCHA_SITE_SECRET"),
    )


def enabled(store):
    _api, site_key, site_secret = config(store)
    return bool(site_key and site_secret)


def widget_html(store, callback):
    """HTML widgetu + <script>; prázdné, když je captcha vypnutá.

    Skript i challenge kanál (WS) se servírují z NAŠÍ domény (proxy v Caddy),
    aby prohlížeč zákazníka nekontaktoval další stranu. Widget si API base
    odvodí z originu skriptu, takže data-api se záměrně neuvádí."""
    _api, site_key, _secret = config(store)
    if not enabled(store):
        return ""
    return (
        '<div data-captcha data-sitekey="%s" data-preset="%s"'
        ' data-callback="%s"></div>\n'
        '<script src="/captcha.min.js" defer></script>'
    ) % (site_key, PRESET, callback)


def verify(store, token):
    """Ověří solve_token u služby. True = projít dál.

    Rozlišuje: chybějící/neplatný token → False; nedostupná služba → True
    (fail-open, viz docstring modulu)."""
    api, _site_key, site_secret = config(store)
    if not (site_secret and api):
        return True  # captcha vypnutá
    if not token or not isinstance(token, str) or len(token) > 512:
        return False
    req = urllib.request.Request(
        api + "/captcha/v1/verify",
        data=json.dumps({"solve_token": token}).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + site_secret,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return bool(json.loads(resp.read()).get("success"))
    except urllib.error.HTTPError as e:
        # 410 = token vypršel/použit, 403 = špatný site_secret → neprojít
        if e.code in (400, 403, 410, 422):
            return False
        return True  # 5xx = výpadek služby → fail-open
    except OSError:
        return True  # timeout / DNS / síť → fail-open


def selftest(api, site_secret):
    """Ověří dosažitelnost služby a platnost site_secret bez řešení challenge:
    pošle zjevně neplatný token — 410 znamená, že secret prošel autentizací,
    403 že je špatný. Vrací (ok, zpráva)."""
    if not api or not site_secret:
        return False, "Chybí API URL nebo site secret."
    req = urllib.request.Request(
        api.rstrip("/") + "/captcha/v1/verify",
        data=json.dumps({"solve_token": "st_selftest_invalid"}).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + site_secret, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, "Služba odpověděla 200 (neočekávané, ale dosažitelná)."
    except urllib.error.HTTPError as e:
        if e.code == 410:
            return True, "Captcha OK — služba běží a site secret je platný."
        if e.code == 403:
            return False, "Site secret služba odmítla (403) — zkontroluj klíč."
        if e.code in (400, 422):
            return True, "Služba běží a secret prošel (odmítla jen testovací token)."
        return False, "Služba odpověděla %d." % e.code
    except OSError as e:
        return False, "Služba nedostupná: %s" % e
