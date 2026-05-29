CREATE TABLE IF NOT EXISTS mail_contacts (
  id BIGSERIAL PRIMARY KEY,
  name TEXT,
  email TEXT NOT NULL,
  note TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_contacts_email_lower
  ON mail_contacts (lower(email));
