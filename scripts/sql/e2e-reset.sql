DO $$ DECLARE tables text; BEGIN IF current_database() <> 'bbd_test' OR current_user <> 'bbd_test' THEN RAISE EXCEPTION 'unsafe test database'; END IF; SELECT string_agg(format('%I.%I', schemaname, tablename), ',') INTO tables FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'; IF tables IS NOT NULL THEN EXECUTE 'TRUNCATE TABLE ' || tables || ' CASCADE'; END IF; END $$;
INSERT INTO github_webhook_capacity (id, digest_count, pending_count) VALUES (1, 0, 0);
INSERT INTO checkpoint_migrations (v) SELECT generate_series(0, 8);
INSERT INTO connector_admission_slots (slot_id) VALUES (1), (2);
INSERT INTO translation_admission_slots (id, translation_id, fencing_token, expires_at) VALUES (1, NULL, 0, NULL);
