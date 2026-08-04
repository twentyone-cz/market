# Obchod — primitivní e-shop s Lightning platbou

Prodejní kanál jednadvacet phone: hardware (krabička, modem) + digitální
zboží (dárkový kredit, dny privátní sítě). Čistá Python stdlib, vzory
z [CockScale](../CockScale) (payments/captcha/admin/settings 1:1).

- **Bez účtů** — objednávka je tajný token v URL.
- **Košík jen v prohlížeči** (localStorage); ceny a dostupnost přepočítává
  výhradně server z DB.
- **Platba LNbits** (invoice-only klíč — aplikace neumí utrácet), settle
  idempotentní + server-side reconcile (zavřená záložka nic nerozbije).
- **Doručení**: výdejní místo (ČR/EU), **anonymní režim** (Balíkovna —
  zákazník nezadává nic, výdejní kód se objeví u objednávky), osobní.
  Doručovací údaje se po dokončení automaticky mažou.
- **Digitální kódy** se generují automaticky po zaplacení; dny privátní
  sítě se registrují u CockScale (fronta s retry — kontrakt viz
  `CockScale/docs/obchod-vouchery-handoff.md`).
- **Compute Captcha** před checkoutem (ekonomická bariéra na vystavování
  invoice), rate limit per IP (jen RAM).

## Běh

Web `:8093` (za nginx na `phone.twentyone.cz/obchod`, `BASE_PATH=/obchod`),
admin `:8094` — **jen LAN**. Tajemství se vkládají v adminu za běhu
(Nastavení): LNbits URL + invoice key ODDĚLENÉ peněženky „obchod",
captcha klíče, CockScale partner secret.

```bash
ADMIN_PASSWORD=... docker compose up -d          # produkce
docker compose -f docker-compose.yml -f docker-compose.lnbits.yml up -d  # dev (FakeWallet)
```

## Testy

```bash
python3 -m unittest discover -s tests -v
```
