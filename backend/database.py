import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/waterflow.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        -- ===== Identity & Access =====
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK (role IN ('super_admin','admin','tenant')),
            unit_id TEXT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            password_hash TEXT,
            created_by TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

        CREATE TABLE IF NOT EXISTS action_tokens (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL CHECK (purpose IN ('invite','password_reset')),
            target_user_id TEXT NOT NULL,
            issued_by TEXT,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS password_reset_requests (
            id TEXT PRIMARY KEY,
            requesting_user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','expired')),
            approved_by TEXT,
            approved_at TEXT,
            resulting_token_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            unit_id TEXT,
            issued_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            revoked INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

        -- ===== Building structure (schema ready now, populated from Phase 2) =====
        CREATE TABLE IF NOT EXISTS units (
            id TEXT PRIMARY KEY,
            unit_number TEXT NOT NULL UNIQUE,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS meters (
            id TEXT PRIMARY KEY,
            dev_eui TEXT NOT NULL UNIQUE,
            unit_id TEXT,
            chirpstack_device_name TEXT,
            status TEXT NOT NULL DEFAULT 'unassigned' CHECK (status IN ('unassigned','active','removed')),
            last_seen_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS meter_status (
            meter_id TEXT PRIMARY KEY,
            last_reading_m3 REAL,
            last_reading_at TEXT,
            battery_voltage REAL,
            battery_low INTEGER,
            valve_confirmed_state TEXT DEFAULT 'unknown'
        );

        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            positive_cumulative_flow_m3 REAL,
            reverse_cumulative_flow_m3 REAL,
            instantaneous_flow_m3h REAL,
            temperature_c REAL,
            battery_voltage REAL,
            battery_low INTEGER,
            flow_sensor_fault INTEGER,
            raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_readings_meter_ts ON readings(meter_id, ts);

        -- ===== Billing (schema ready now, populated from Phase 3) =====
        CREATE TABLE IF NOT EXISTS billing_config (
            unit_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'monthly' CHECK (mode IN ('prepaid','monthly')),
            tariff_override_rand_per_kl REAL,
            baseline_reading_m3 REAL,
            target_reading_m3 REAL,
            balance_liters_remaining REAL,
            low_balance_threshold_liters REAL,
            cycle_start_day INTEGER DEFAULT 1,
            cycle_length_days INTEGER DEFAULT 30,
            current_cycle_start_reading_m3 REAL,
            current_cycle_start_date TEXT,
            valve_target_state TEXT DEFAULT 'open',
            valve_command_pending INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS topup_transactions (
            id TEXT PRIMARY KEY,
            unit_id TEXT NOT NULL,
            amount_rand REAL,
            liters_purchased REAL,
            tariff_used REAL,
            baseline_before REAL,
            target_after REAL,
            recorded_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS billing_periods (
            id TEXT PRIMARY KEY,
            unit_id TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            start_reading_m3 REAL,
            end_reading_m3 REAL,
            consumption_m3 REAL,
            tariff_used REAL,
            amount_due_rand REAL,
            status TEXT DEFAULT 'open' CHECK (status IN ('open','closed','invoiced')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS valve_commands (
            id TEXT PRIMARY KEY,
            meter_id TEXT NOT NULL,
            command TEXT NOT NULL CHECK (command IN ('open','close')),
            reason TEXT NOT NULL,
            requested_by TEXT,
            requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'queued',
            delivered_at TEXT,
            confirmed_at TEXT
        );

        -- ===== Audit & Digest =====
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP,
            actor_user_id TEXT,
            actor_role TEXT,
            action_type TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            before_value TEXT,
            after_value TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS digest_log (
            meter_id TEXT NOT NULL,
            digest_date TEXT NOT NULL,
            reading_used_m3 REAL,
            reading_used_at TEXT,
            was_stale INTEGER DEFAULT 0,
            PRIMARY KEY (meter_id, digest_date)
        );

        -- ===== ChirpStack bootstrap cache (Phase 2) =====
        CREATE TABLE IF NOT EXISTS chirpstack_cache (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- ===== Runtime settings (replaces HA addon options.json) =====
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()
