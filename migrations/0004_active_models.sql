CREATE TABLE active_models(competition_code TEXT PRIMARY KEY, model_version_id INTEGER NOT NULL REFERENCES model_versions(model_version_id));
