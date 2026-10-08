DO $$ DECLARE tables text; BEGIN IF current_database() <> 'bbd_test' OR current_user <> 'bbd_test' THEN RAISE EXCEPTION 'unsafe test database'; END IF; SELECT string_agg(format('%I.%I', schemaname, tablename), ',') INTO tables FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'; IF tables IS NOT NULL THEN EXECUTE 'TRUNCATE TABLE ' || tables || ' CASCADE'; END IF; END $$;
INSERT INTO p12_backup_control (id, epoch, phase) VALUES (1, 1, 'idle');
INSERT INTO github_webhook_capacity (id, digest_count, pending_count) VALUES (1, 0, 0);
INSERT INTO checkpoint_migrations (v) SELECT generate_series(0, 8);
