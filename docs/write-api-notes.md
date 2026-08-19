# FPL write API — captured contract notes

Findings from real browser captures (Chrome DevTools, 2026-08-19,
pre-season before the GW1 deadline). Tokens/cookies redacted. These
captures win over any assumed payload shape.

## Common request headers (confirmed on entry-create, entry-autopick, and my-team)

The web app sends, alongside the usual browser headers:

```
content-type: application/json
origin: https://fantasy.premierleague.com
referer: https://fantasy.premierleague.com/en/<page>   (full page path)
x-api-authorization: Bearer <access token>
x-api-language: en
x-csrftoken:                                            (present but EMPTY)
```

Notably **no `X-Requested-With` header** (the pre-capture assumption
included it; removed). Auth cookies are not required by our client — the
Bearer header carries authentication.

## Pre-season: initial squad creation (CONFIRMED, out of scope for v1)

Before the first deadline the squad is created/edited through dedicated
endpoints — **not** `/api/transfers/`:

- `POST /api/entry-autopick/` — body `{"existing_elements": [<ids>]}`,
  returns a full 15-pick suggestion with `position`, `is_captain`,
  `is_vice_captain` per pick.
- `POST /api/entry-create/` — creates the entry. Body:

  ```json
  {
    "email": false,
    "favourite_team": 14,
    "name": "<team name>",
    "terms_agreed": true,
    "picks": [{"element": 529, "purchase_price": 50}, ...]
  }
  ```

  15 picks, no positions/captaincy in this call. Response: `201` with an
  empty body.

The write tools in this fork target the in-season contract only; the
pre-season path above is documented so nobody mistakes one for the other.

## In-season: lineup/captain save (CONFIRMED)

Captured from Pick Team → Save Your Team (captain change, 2026-08-19):

- `POST /api/my-team/{entry_id}/`
- Referer: `https://fantasy.premierleague.com/en/my-team`
- Body: the FULL picks array, even for a captain-only change:

  ```json
  {
    "chip": null,
    "picks": [
      {"element": 529, "position": 1,
       "is_captain": false, "is_vice_captain": true},
      ... (all 15, positions 1-15)
    ]
  }
  ```

This matches the implemented `_submit_picks` payload exactly.

## Transfers (CONFIRMED by live execution, 2026-08-19 pre-season)

`POST /api/transfers/` with
`{"chip": null, "entry": <id>, "event": <gw>, "transfers": [{element_in,
element_out, purchase_price, selling_price}]}` was executed live before
the GW1 deadline (Tarkowski → Senesi) and accepted; the squad change was
verified by re-reading the my-team endpoint. Notably the endpoint DOES
work pre-season, contrary to the initial worry that pre-deadline edits
might route elsewhere. Still unobserved in the wild: in-season points-hit
handling and the chip field carrying a non-null value (wildcard/freehit).
