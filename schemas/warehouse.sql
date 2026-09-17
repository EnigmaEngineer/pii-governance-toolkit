-- The sample warehouse, generated from pii/schema.py.
-- Edit that module rather than this file, which is overwritten.

CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE raw.patient (
  patient_id BIGINT NOT NULL,
  mrn VARCHAR NOT NULL,
  first_name VARCHAR,
  last_name VARCHAR,
  email VARCHAR,
  phone VARCHAR,
  street_address VARCHAR,
  city VARCHAR,
  postal_code VARCHAR,
  birth_date DATE,
  sex VARCHAR,
  ssn VARCHAR,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE raw.encounter (
  encounter_id BIGINT NOT NULL,
  patient_id BIGINT NOT NULL,
  admitted_at TIMESTAMP NOT NULL,
  discharged_at TIMESTAMP,
  department VARCHAR,
  attending_npi VARCHAR,
  primary_diagnosis VARCHAR,
  clinical_note VARCHAR,
  disposition VARCHAR
);

CREATE TABLE raw.claim (
  claim_id BIGINT NOT NULL,
  encounter_id BIGINT NOT NULL,
  member_number VARCHAR,
  payer_name VARCHAR,
  billed_amount DECIMAL(12,2),
  paid_amount DECIMAL(12,2),
  claim_status VARCHAR,
  submitted_on DATE
);

CREATE TABLE raw.device_reading (
  reading_id BIGINT NOT NULL,
  patient_id BIGINT NOT NULL,
  device_serial VARCHAR,
  taken_at TIMESTAMP NOT NULL,
  metric VARCHAR,
  reading_value DOUBLE,
  source_ip VARCHAR
);

CREATE TABLE analytics.encounter_daily (
  day DATE NOT NULL,
  department VARCHAR NOT NULL,
  postal_code VARCHAR,
  encounters BIGINT NOT NULL,
  mean_length_of_stay_h DOUBLE
);
