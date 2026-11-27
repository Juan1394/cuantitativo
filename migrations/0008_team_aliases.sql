-- Seed mínimo y verificable de E0: cinco equipos presentes en los fixtures de
-- proveedores documentados para esta fase, con los alias suministrados para
-- football-data.co.uk, football-data.org y The Odds API. No se infieren más equipos.
CREATE TABLE team_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    competition_code TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('football_data_co_uk','football_data_org','the_odds_api','api_football','manual')),
    status TEXT NOT NULL CHECK(status IN ('confirmed','pending','rejected')) DEFAULT 'pending',
    fuzzy_score REAL,
    created_at_utc TEXT NOT NULL,
    verified_at_utc TEXT,
    UNIQUE(source, competition_code, raw_name),
    CHECK(status != 'confirmed' OR verified_at_utc IS NOT NULL),
    CHECK(fuzzy_score IS NULL OR (fuzzy_score > 0 AND source != 'manual'))
);

CREATE INDEX idx_team_aliases_canonical ON team_aliases(canonical_name);
CREATE INDEX idx_team_aliases_lookup ON team_aliases(competition_code, source, raw_name);
CREATE INDEX idx_team_aliases_status ON team_aliases(status);

-- Alias confirmados de los cinco equipos E0 indicados en la especificación.
INSERT INTO team_aliases
    (canonical_name, raw_name, competition_code, source, status, created_at_utc, verified_at_utc)
VALUES
    ('Manchester United', 'Man Utd', 'E0', 'football_data_co_uk', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Manchester United', 'Manchester United FC', 'E0', 'football_data_org', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Manchester United', 'Manchester United', 'E0', 'the_odds_api', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Manchester City', 'Man City', 'E0', 'football_data_co_uk', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Manchester City', 'Manchester City FC', 'E0', 'football_data_org', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Manchester City', 'Manchester City', 'E0', 'the_odds_api', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Arsenal', 'Arsenal', 'E0', 'football_data_co_uk', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Arsenal', 'Arsenal FC', 'E0', 'football_data_org', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Arsenal', 'Arsenal', 'E0', 'the_odds_api', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Liverpool', 'Liverpool', 'E0', 'football_data_co_uk', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Liverpool', 'Liverpool FC', 'E0', 'football_data_org', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Liverpool', 'Liverpool', 'E0', 'the_odds_api', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Chelsea', 'Chelsea', 'E0', 'football_data_co_uk', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Chelsea', 'Chelsea FC', 'E0', 'football_data_org', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('Chelsea', 'Chelsea', 'E0', 'the_odds_api', 'confirmed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
