CREATE TABLE model_artifacts(model_version_id INTEGER PRIMARY KEY REFERENCES model_versions(model_version_id), model_path TEXT NOT NULL);
