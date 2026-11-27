CREATE TABLE rate_limit_budget (
    provider TEXT NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('minute', 'day', 'month')),
    used INTEGER NOT NULL,
    limit_value INTEGER NOT NULL,
    reset_at_utc TEXT NOT NULL,
    PRIMARY KEY (provider, window)
);
