-- 0002: R6.4 requires every match decision to record parser/config version;
-- the 4.1 key-field list omits it, so add it here.
ALTER TABLE entity_match_decisions ADD COLUMN parser_version TEXT DEFAULT '';
