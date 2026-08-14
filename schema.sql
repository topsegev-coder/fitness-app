-- ============================================================================
-- Smart Fitness Tracker — Database Schema
-- Target: PostgreSQL 14+
-- (Fully SQLite-compatible with 2 tweaks noted inline — see bottom of file)
-- ============================================================================

CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(50) UNIQUE NOT NULL,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE equipment_types (
    code            VARCHAR(20) PRIMARY KEY,
    description     VARCHAR(100)
);

INSERT INTO equipment_types (code, description) VALUES
    ('FREE_WEIGHT', 'Dumbbells, barbells, kettlebells'),
    ('MACHINE',     'Selectorized / plate-loaded machines'),
    ('BODYWEIGHT',  'Bodyweight, optionally with added weight vest'),
    ('BAND',        'Resistance bands (discrete resistance levels, not kg)');

CREATE TABLE exercises (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(120) NOT NULL,
    equipment_type      VARCHAR(20) NOT NULL REFERENCES equipment_types(code),
    increment_step      NUMERIC(6,2) NOT NULL DEFAULT 2.5 CHECK (increment_step >= 0),
    min_reps_target     SMALLINT NOT NULL DEFAULT 8,
    max_reps_target      SMALLINT NOT NULL DEFAULT 12 CHECK (max_reps_target >= min_reps_target),
    max_weight_limit    NUMERIC(6,2),
    default_sets        SMALLINT NOT NULL DEFAULT 3,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_exercises_equipment_type ON exercises(equipment_type);

CREATE TABLE routines (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(120) NOT NULL,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE routine_exercises (
    id                      SERIAL PRIMARY KEY,
    routine_id              INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
    exercise_id             INTEGER NOT NULL REFERENCES exercises(id) ON DELETE RESTRICT,
    display_order           SMALLINT NOT NULL DEFAULT 0,
    prescribed_weight       NUMERIC(6,2) NOT NULL DEFAULT 0,
    prescribed_reps_target  SMALLINT NOT NULL,
    prescribed_sets         SMALLINT NOT NULL,
    consecutive_easy_count  SMALLINT NOT NULL DEFAULT 0,
    target_type TEXT NOT NULL DEFAULT 'reps',
    UNIQUE (routine_id, exercise_id)
);

CREATE INDEX idx_routine_exercises_routine ON routine_exercises(routine_id);

CREATE TABLE workout_sessions (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    routine_id      INTEGER REFERENCES routines(id) ON DELETE SET NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    notes           TEXT
);

CREATE INDEX idx_sessions_user_started ON workout_sessions(user_id, started_at DESC);

CREATE TABLE workout_logs (
    id                  SERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    routine_exercise_id INTEGER NOT NULL REFERENCES routine_exercises(id) ON DELETE RESTRICT,
    set_number          SMALLINT NOT NULL,
    reps_performed      SMALLINT NOT NULL,
    weight_used         NUMERIC(6,2) NOT NULL,
    rpe_score           NUMERIC(3,1) CHECK (rpe_score BETWEEN 1 AND 10),
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, routine_exercise_id, set_number)
);

CREATE INDEX idx_logs_routine_exercise_time ON workout_logs(routine_exercise_id, logged_at DESC);