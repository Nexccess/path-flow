PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  industry TEXT,
  started_at TEXT,
  status TEXT NOT NULL DEFAULT 'DRAFT',
  followup1_days INTEGER NOT NULL DEFAULT 4,
  followup2_days INTEGER NOT NULL DEFAULT 5,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS leads (
  campaign_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  store_name TEXT NOT NULL,
  area TEXT,
  industry TEXT,
  address TEXT,
  phone TEXT,
  store_url TEXT,
  lp_url TEXT,
  contact_channel TEXT,
  contact_address TEXT,
  screening_status TEXT NOT NULL DEFAULT 'PENDING',
  sales_status TEXT NOT NULL DEFAULT 'READY',
  response_type TEXT,
  human_action INTEGER NOT NULL DEFAULT 0,
  initial_sent_at TEXT,
  followup1_sent_at TEXT,
  followup2_sent_at TEXT,
  response_at TEXT,
  closed_at TEXT,
  close_reason TEXT,
  variant TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (campaign_id, store_id),
  FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  external_message_id TEXT,
  payload TEXT,
  UNIQUE(campaign_id, store_id, event_type, external_message_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(campaign_id, sales_status);
CREATE INDEX IF NOT EXISTS idx_leads_human_action ON leads(campaign_id, human_action);
CREATE INDEX IF NOT EXISTS idx_events_store ON events(campaign_id, store_id, event_at);
