-- 006_gui_bridge_persistence.sql
-- Minimal persistence needed to connect the existing Dalton wizard to Perch
-- without duplicating customer data-entry screens.

-- Perch requires a billing/mailing address in addition to the utility account's
-- service address. The legacy Dalton GUI only had the service address, so these
-- fields preserve the rep's explicit "same as service" choice or a different
-- mailing address when provided.
ALTER TABLE enrollments ADD COLUMN billing_same_as_service INTEGER NOT NULL DEFAULT 1;
ALTER TABLE enrollments ADD COLUMN billing_street TEXT;
ALTER TABLE enrollments ADD COLUMN billing_unit TEXT;
ALTER TABLE enrollments ADD COLUMN billing_city TEXT;
ALTER TABLE enrollments ADD COLUMN billing_state TEXT;
ALTER TABLE enrollments ADD COLUMN billing_zip TEXT;

-- Certain Perch utilities require a POD/secondary identifier. Keep it beside
-- the utility account rather than in transient browser state.
ALTER TABLE utility_accounts ADD COLUMN secondary_account_identifier TEXT;
