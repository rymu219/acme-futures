"""Warden — analyst agent for the new-fleet runtime.

Runs out-of-process (Railway-hosted), reads Supabase, writes structured
events to the `operator_events` table. Never connects to the broker.
Permissions enforced at the DB role level: read-only on trading tables,
write-only on operator_events.

The v1 surface is small — one monitor (heartbeat staleness), one CLI
runner. The original Part-2 plan called for more (cluster firing,
daily brief via Resend, weekly Sunday packet, Anthropic-backed Q&A
endpoint). Those are layered on top once this foundation is stable
and you've decided on the email / scheduling stack.

Lives at the repo root (not under `src/acme/`) because Warden is
intentionally a separate service deployable independently — it
doesn't need to import the runner / strategies / conductor code.
The only shared dependency is the Supabase schema.
"""
