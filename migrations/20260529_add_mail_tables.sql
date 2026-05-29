CREATE TABLE IF NOT EXISTS mail_inbox (
  id BIGSERIAL PRIMARY KEY,
  from_addr TEXT NOT NULL,
  from_name TEXT,
  subject TEXT,
  body TEXT,
  summary TEXT,
  received_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mail_outbox (
  id BIGSERIAL PRIMARY KEY,
  to_addr TEXT NOT NULL,
  to_name TEXT,
  subject TEXT,
  body TEXT,
  summary TEXT,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  sent_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mail_inbox_from_received
  ON mail_inbox (from_addr, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_mail_outbox_to_created
  ON mail_outbox (to_addr, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mail_outbox_status_created
  ON mail_outbox (status, created_at DESC);
