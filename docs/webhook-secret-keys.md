# Webhook secret keys

Outbound webhook destinations (DS-415) hold a secret each: the signing secret
of a generic destination, an ntfy access token or a Gotify app token. They are
stored encrypted in `webhook_destinations` (AES-256-GCM, a random nonce per
value, and the id of the key that sealed it in `secret_key_id`). The api
encrypts when a destination is added; the notifier decrypts when it sends.
Both read the same key ring from `WEBHOOK_SECRET_KEYS`, from a Secret you
provision. The chart never generates one: a key lost with a release would
take every stored secret with it.

Without a usable ring, `api.webhooks.enabled` and `notifier.webhooks.enabled`
have no effect: the api keeps its routes off and the notifier sends nothing,
each logging an error that names `WEBHOOK_SECRET_KEYS`. Nothing is ever stored
or sent unencrypted, and neither service logs a key or a secret.

## The value

```text
WEBHOOK_SECRET_KEYS=<id>:<base64 of 32 random bytes>[,<id>:<base64>...]
```

The id is 1 to 32 letters, digits, `-` or `_`. The first key encrypts; every
key decrypts. Ids are not secret.

## Make the first key

```sh
KEY="k1:$(openssl rand -base64 32)"
kubectl -n distant-signal create secret generic webhook-secret-keys \
  --from-literal=webhook-secret-keys="${KEY}"
```

In production the Secret is a SealedSecret named `webhook-secret-keys`, key
`webhook-secret-keys`, in `distant-signal`: seal the value byte for byte
(`--from-file`, no trailing newline). Then set in the chart's values:

```yaml
api:
  webhooks:
    secretKeys:
      existingSecret: webhook-secret-keys
      existingSecretKey: webhook-secret-keys
notifier:
  webhooks:
    secretKeys:
      existingSecret: webhook-secret-keys
      existingSecretKey: webhook-secret-keys
```

Turn on, in this order, once ds-core v0.14.2 or later is migrated:
`notifier.webhooks` (with `networkPolicy.egress.enabled`), then
`api.webhooks`. Check the logs for `webhook secret keys loaded` (it lists key
ids only) in both.

## Rotate

1. Make a new key and put it first, keeping the old one:
   `k2:<new>,k1:<old>`. Roll the notifier first, then the api, so the notifier
   can already read what the api starts writing with `k2`.
2. New and changed destinations are sealed with `k2`. Rows sealed with `k1`
   still decrypt. See which keys are in use (read-only):
   ```sql
   SELECT secret_key_id, count(*) FROM webhook_destinations
   WHERE secret_key_id IS NOT NULL GROUP BY 1;
   ```
3. Retire `k1` only when no row names it. There is no bulk re-encrypt: a
   destination's secret is re-sealed when its user deletes and re-adds it. If
   `k1` has to go sooner (it leaked), remove it from the ring and ask the
   affected users to re-add their destinations: a destination whose key is not
   in the ring is skipped by the notifier (error log, and
   `notifier_webhook_send_total{outcome="undecryptable"}`), never turned off.

## If a key is lost or leaks

Lost: the stored secrets are unrecoverable. Remove the affected rows
(`DELETE FROM webhook_destinations WHERE secret_key_id = '<id>'`) and tell the
users to add their destinations again. Leaked: rotate as above, and treat the
secrets sealed under it as exposed to whoever also had database access: users
should re-create their destinations (a generic destination gets a new signing
secret) and revoke the tokens at ntfy or Gotify.
