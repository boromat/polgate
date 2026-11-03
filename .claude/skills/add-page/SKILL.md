---
name: add-page
description: Add a hub page or HTMX partial (route + Jinja2 template + i18n keys + auth + test) following the repo conventions. Use when asked to build any UI screen, form, list, panel or SSE endpoint in polgate.
argument-hint: <page or feature>
---

# Add hub page: $ARGUMENTS

Stack: FastAPI + Jinja2 + HTMX. No frontend build.

**Nothing inline.** No `<script>` block, no `on*=` handler, no `hx-on`, no `style=` attribute in a
template: declare behaviour with a `data-` hook and handle it in `polgate/static/app.js`, and put styling
in `style.css`. That is what keeps `unsafe-inline` and `unsafe-eval` out of the Content-Security-Policy,
and `tests/test_security.py` fails if a template breaks the rule.

## Steps
1. **Router**: pick the area file in `polgate/web/` (`auth`, `chat`, `files`, `admin`). Routes return
   `render(request, "template.html", **ctx)` from `polgate.rendering`. Protect with
   `user: User = Depends(current_user)` or `Depends(require_admin)`. DB via `session: Session = Depends(get_session)`.
2. **Templates**: full pages extend `base.html` in `polgate/templates/`; HTMX responses return partials from
   `polgate/templates/partials/`. Use `hx-post`, `hx-target`, `hx-swap`. Read a stream with `EventSource`
   in `polgate/static/app.js` behind a `data-stream` hook, as `partials/message_pair.html` does — the
   HTMX SSE extension only fires for events named in `sse-swap`, and without one the live answer stayed
   blank. Close the source on the last event. When a loop includes a partial, bind the loop variable to
   the name the partial reads — `{% for file in files %}`, not `{% for f in files %}` — because Jinja
   raises at render time and the page becomes a 500.
3. **i18n**: every visible string is `{{ t("area.key") }}`. Add the key to BOTH `polgate/i18n/pl.json` and
   `en.json` in the same commit. Polish is the primary language; keep English natural, not literal.
4. **Never render raw PII where the policy says tokens**: previews show `content_sent`/`text_sent` (tokens)
   in the "sent" column and `decrypt(content_original_enc)` only for the owning user. Wrap tokens in
   `<span class="token">` with the `highlight_tokens` filter from `polgate/rendering.py`, which uses
   `polanon.policy.TOKEN_PATTERN` and autoescapes the rest with `markupsafe.escape`.
5. **Forms**: `python-multipart` is installed; use `Form(...)` params. On validation error re-render the page
   with `error=...` (translated key), status 400 for HTMX partials so the swap still happens.
6. **Audit**: mutations that matter (login, policy change, file approve, user create) call `audit.log(...)`
   with counts/ids only.
7. **Test** in `tests/test_web_<area>.py` using `httpx.AsyncClient(transport=ASGITransport(app=app))`: anonymous
   -> redirect to `/login`, wrong role -> 403, happy path renders the expected i18n string in both languages
   (`?lang=en`). A page that can list something must have a test that loads it with something in the list.
8. Run `/quality-gate`, commit `feat(web): ...`.
