PRAGMA foreign_keys=OFF;
CREATE TABLE snapshots_new(snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL REFERENCES events(event_id), market TEXT NOT NULL, selection TEXT NOT NULL, odds REAL NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('valid','gap','skipped')), skip_reason TEXT CHECK(skip_reason IN ('daily_cap_reached','too_far_from_kickoff','budget_exhausted','no_odds_available','league_not_in_response','api_error','dry_run_disabled','team_not_confirmed')), captured_at_utc TEXT NOT NULL);
INSERT INTO snapshots_new SELECT * FROM snapshots;
DROP TABLE snapshots;
ALTER TABLE snapshots_new RENAME TO snapshots;
CREATE INDEX idx_snapshots_event_captured ON snapshots(event_id, captured_at_utc);
CREATE INDEX idx_snapshots_status ON snapshots(status);
PRAGMA foreign_keys=ON;
PRAGMA foreign_key_check;
