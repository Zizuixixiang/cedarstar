ALTER TABLE summaries
  ADD COLUMN IF NOT EXISTS archived_by INTEGER;

ALTER TABLE summaries
  ADD COLUMN IF NOT EXISTS is_starred BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_summaries_archived_by'
  ) THEN
    ALTER TABLE summaries
      ADD CONSTRAINT fk_summaries_archived_by
      FOREIGN KEY (archived_by) REFERENCES summaries(id)
      ON DELETE SET NULL;
  END IF;
END $$;

ALTER TABLE longterm_memories
  ADD COLUMN IF NOT EXISTS source_chunk_ids JSONB;

ALTER TABLE longterm_memories
  ADD COLUMN IF NOT EXISTS is_starred BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_summaries_archived_by
  ON summaries (archived_by);

CREATE INDEX IF NOT EXISTS idx_summaries_is_starred
  ON summaries (is_starred);

CREATE INDEX IF NOT EXISTS idx_longterm_source_chunks
  ON longterm_memories USING GIN (source_chunk_ids);

CREATE INDEX IF NOT EXISTS idx_longterm_is_starred
  ON longterm_memories (is_starred);

INSERT INTO config (key, value, updated_at)
VALUES
  ('context_archived_daily_limit', '3', NOW()),
  ('archived_daily_min_hits', '2', NOW()),
  ('starred_boost_factor', '1.2', NOW())
ON CONFLICT (key) DO NOTHING;
