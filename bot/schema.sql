-- Stylework B2B Sales CRM schema (Neon Postgres)
-- Run once via scripts/apply_schema.py against DATABASE_URL.

CREATE TYPE user_role AS ENUM ('rep', 'manager');

CREATE TYPE lead_stage AS ENUM (
    'Inquiry', 'Qualified', 'Site Visit', 'Proposal', 'Negotiation',
    'Closed-Won', 'Closed-Lost'
);

CREATE TYPE space_type AS ENUM (
    'Dedicated Desk', 'Private Cabin', 'Managed Office', 'Day Pass'
);

CREATE TYPE interaction_type AS ENUM (
    'whatsapp_forward', 'voice_note', 'screenshot', 'addnote_command'
);

CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    first_name TEXT NOT NULL,
    email TEXT,
    company TEXT,
    role user_role NOT NULL DEFAULT 'rep',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE companies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT,
    city TEXT
);

CREATE TABLE leads (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    contact_name TEXT NOT NULL,
    contact_role TEXT,
    phone TEXT,
    stage lead_stage NOT NULL DEFAULT 'Inquiry',
    seat_count INTEGER,
    city TEXT,
    space_type space_type,
    budget_per_seat NUMERIC(10, 2),
    est_deal_value NUMERIC(12, 2),
    move_in_date TEXT,
    assigned_to BIGINT REFERENCES users(id) ON DELETE SET NULL,
    source TEXT,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE interactions (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    type interaction_type NOT NULL,
    raw_content TEXT,
    ai_summary TEXT,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE spaces (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    locality TEXT,
    total_seats INTEGER NOT NULL,
    available_seats INTEGER NOT NULL,
    price_per_seat NUMERIC(10, 2),
    space_type space_type NOT NULL
);

CREATE INDEX idx_leads_assigned_to ON leads(assigned_to);
CREATE INDEX idx_leads_stage ON leads(stage);
CREATE INDEX idx_leads_last_activity_at ON leads(last_activity_at);
CREATE INDEX idx_interactions_lead_id ON interactions(lead_id);

-- Webhook delivery claims and PTB conversation state (migrations/2026-10-24-webhook-state.sql).
CREATE TABLE telegram_updates (
    update_id   bigint PRIMARY KEY,
    status      text NOT NULL CHECK (status IN ('processing', 'done')),
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ptb_state (
    kind       text NOT NULL,
    key        text NOT NULL,
    data       jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
);
