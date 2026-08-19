# FPL write API — captured contract notes

Findings from real browser captures (Chrome DevTools, 2026-08-19,
pre-season before the GW1 deadline). Tokens/cookies redacted. These
captures win over any assumed payload shape.

## Common request headers (confirmed on entry-create and entry-autopick)

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

## In-season: lineup/captain save (PENDING CAPTURE)

Expected `POST /api/my-team/{entry_id}/` with
`{"chip": ..., "picks": [{element, position, is_captain, is_vice_captain}]}`.
To be confirmed by capturing the Pick Team → Save request.

## In-season: transfers (PENDING CAPTURE — only possible after GW1 deadline)

Expected `POST /api/transfers/` with
`{"chip": ..., "entry": ..., "event": ..., "transfers": [{element_in,
element_out, purchase_price, selling_price}]}`. To be confirmed after the
first deadline passes.
