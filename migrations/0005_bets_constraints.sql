CREATE TABLE bets_new(bet_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL REFERENCES events(event_id), market TEXT NOT NULL, selection TEXT NOT NULL, manual_odds REAL NOT NULL CHECK(typeof(manual_odds) IN ('real','integer') AND manual_odds > 1.0), stake REAL NOT NULL CHECK(typeof(stake) IN ('real','integer') AND stake > 0), ev REAL NOT NULL, model_probability REAL NOT NULL CHECK(typeof(model_probability) IN ('real','integer') AND model_probability > 0 AND model_probability < 1), status TEXT NOT NULL CHECK(status IN ('open','won','lost','void','cancelled')), idempotency_key TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL, closed_at_utc TEXT);
INSERT INTO bets_new (bet_id, event_id, market, selection, manual_odds, stake, ev, model_probability, status, idempotency_key, created_at_utc, closed_at_utc) SELECT bet_id, event_id, market, selection, manual_odds, stake, ev, model_probability, status, idempotency_key, created_at_utc, closed_at_utc FROM bets;
DROP TABLE bets;
ALTER TABLE bets_new RENAME TO bets;
UPDATE sqlite_sequence SET seq = (SELECT COALESCE(MAX(bet_id), 0) FROM bets) WHERE name = 'bets';
INSERT INTO sqlite_sequence (name, seq) SELECT 'bets', (SELECT COALESCE(MAX(bet_id), 0) FROM bets) WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'bets');
CREATE INDEX bets_status_idx ON bets(status);
