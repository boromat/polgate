# Security policy

This project handles personal data by design. If you find a way for original (unmasked) values to reach
an LLM provider, to be written to logs/audit, or to be read by another user, please report it privately
from the repository's **Security** tab (*Report a vulnerability*), or write to kontaktboromat@gmail.com,
instead of opening a public issue. A public issue would publish the way in before there is a fix. You
will get an acknowledgement within 7 days.

Scope of the guarantee (`docs/THREAT_MODEL.md` has the long version):
- Only tokenized text is sent to providers. Enforced in code and tests, not by a prompt.
- Originals are encrypted at rest (Fernet) and are deleted on the admin's retention schedule.
- The browser side is in scope too: script injection into a page, a cross-site request that changes
  state, a session cookie that survives something it should not, or a redirect that leaves the hub.
- Detection of names and places is statistical; a missed entity is a detection gap, not a bypass, but we
  still want to know. Detection is done by [polanon](https://github.com/boromat/polanon), so report a gap
  there, where a case in its benchmark demonstrates it reproducibly. A way past the masking that does not
  depend on the hub is reported privately to polanon, as its
  [security policy](https://github.com/boromat/polanon/blob/main/SECURITY.md) describes.
