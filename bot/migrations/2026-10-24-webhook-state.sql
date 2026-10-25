-- Webhook delivery claims and PTB conversation state, for the Vercel rehost (2026-10-24).
-- Additive only. Safe to run more than once.

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id   bigint PRIMARY KEY,
    status      text NOT NULL CHECK (status IN ('processing', 'done')),
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ptb_state (
    kind       text NOT NULL,
    key        text NOT NULL,
    data       jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
);
