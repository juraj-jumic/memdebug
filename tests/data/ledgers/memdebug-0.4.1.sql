-- memdebug 0.4.1; user_version=3; events=7
BEGIN TRANSACTION;
CREATE TABLE blobs (hash TEXT PRIMARY KEY, text TEXT NOT NULL);
INSERT INTO "blobs" VALUES('84925127924d6bfb40f9d9a29f9bfe95c9027d2d72966e69c56e0f2bc06bc2de','has a dog called Rex
');
INSERT INTO "blobs" VALUES('e97db6a7402fda801f2e18ecb8b393ca05dc97d02963a8756541783224a3bbaf','likes tea
');
INSERT INTO "blobs" VALUES('1464212e01e44c6d1f7e1906fd7bf79304bba267b866c9c7fe8e5ce4c1907895','waters the fern on Sundays
');
INSERT INTO "blobs" VALUES('dbd001223420711d7ee2977345ae3c2bc090b2a320cfbedd72b8e11e83060966','likes green tea
');
CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, backend TEXT NOT NULL, ref TEXT, snap TEXT, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, op TEXT, trust TEXT);
INSERT INTO "events" VALUES(1,'e1','folder',NULL,NULL,'{"after":"has a dog called Rex\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"pets.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:38.178445Z","ts_observed":true}','0000000000000000000000000000000000000000000000000000000000000000','afa9c4838b7d2aaec17afaeb5f77cf24a992be241c6d2460c6ec264fa15d57c2','ADD','unknown');
INSERT INTO "events" VALUES(2,'e2','folder',NULL,NULL,'{"after":"likes tea\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"tea.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:38.178445Z","ts_observed":true}','afa9c4838b7d2aaec17afaeb5f77cf24a992be241c6d2460c6ec264fa15d57c2','3d5c6792cdc04b3e03883474fd72ac6736319d9850cc468649797fbd2975ccaf','ADD','unknown');
INSERT INTO "events" VALUES(3,'e3','folder',NULL,'SNAPSHOT:s1','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"acbee0ee42fd6cdd44c547194fb990462a78b393e9895b4cbd7c72bebad99bf3\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s1","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:38.183590Z","ts_observed":false}','3d5c6792cdc04b3e03883474fd72ac6736319d9850cc468649797fbd2975ccaf','fb92ff397959996b31e59776046839ad1719c10b98e0ffe10c1aa0d1043330be','SNAPSHOT','unknown');
INSERT INTO "events" VALUES(4,'e4','folder',NULL,NULL,'{"after":"likes green tea\n","backend":"folder","backend_ref":null,"before":"likes tea\n","memory_id":"tea.md","op":"UPDATE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:38.756594Z","ts_observed":true}','fb92ff397959996b31e59776046839ad1719c10b98e0ffe10c1aa0d1043330be','83d5d40865912d09607e2d5a56dc9f84ac74b5448e20465cc80c83f7b1f46bfe','UPDATE','unknown');
INSERT INTO "events" VALUES(5,'e5','folder',NULL,NULL,'{"after":"waters the fern on Sundays\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"plants.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:39.036463Z","ts_observed":true}','83d5d40865912d09607e2d5a56dc9f84ac74b5448e20465cc80c83f7b1f46bfe','7aca99cc86eca3b2c91d1368b24412a10f102389b8311258f6d0238dfed5ef63','ADD','unknown');
INSERT INTO "events" VALUES(6,'e6','folder',NULL,NULL,'{"after":null,"backend":"folder","backend_ref":null,"before":"has a dog called Rex\n","memory_id":"pets.md","op":"DELETE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:39.322331Z","ts_observed":true}','7aca99cc86eca3b2c91d1368b24412a10f102389b8311258f6d0238dfed5ef63','2048230803edb6c0b1b926d5127da42f211f83a86160df77084f86d43f19dae2','DELETE','unknown');
INSERT INTO "events" VALUES(7,'e7','folder',NULL,'SNAPSHOT:s2','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"8b00bf6a0ff60d2dd15fcf736139c8a9e9f63b47d8b98fa520685110b45d66aa\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s2","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:39.616417Z","ts_observed":false}','2048230803edb6c0b1b926d5127da42f211f83a86160df77084f86d43f19dae2','3803b30f7cd99276e0249e5f32262a813b6d00793bdd4a6601dbb8f34ac92131','SNAPSHOT','unknown');
CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL, memory_id TEXT NOT NULL, hash TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY (snapshot_id, memory_id));
INSERT INTO "snapshot_entries" VALUES(1,'pets.md','84925127924d6bfb40f9d9a29f9bfe95c9027d2d72966e69c56e0f2bc06bc2de','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(1,'tea.md','e97db6a7402fda801f2e18ecb8b393ca05dc97d02963a8756541783224a3bbaf','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(2,'plants.md','1464212e01e44c6d1f7e1906fd7bf79304bba267b866c9c7fe8e5ce4c1907895','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(2,'tea.md','dbd001223420711d7ee2977345ae3c2bc090b2a320cfbedd72b8e11e83060966','{"store":"notes"}');
CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL, scope TEXT NOT NULL, taken_at TEXT NOT NULL, ledger_seq INTEGER NOT NULL, ledger_head TEXT NOT NULL, complete INTEGER NOT NULL, label TEXT, count INTEGER NOT NULL, entries_hash TEXT NOT NULL);
INSERT INTO "snapshots" VALUES(1,'folder','{"store":"notes"}','2026-10-11T08:43:38.183590+00:00',2,'3d5c6792cdc04b3e03883474fd72ac6736319d9850cc468649797fbd2975ccaf',1,'baseline',2,'acbee0ee42fd6cdd44c547194fb990462a78b393e9895b4cbd7c72bebad99bf3');
INSERT INTO "snapshots" VALUES(2,'folder','{"store":"notes"}','2026-10-11T08:43:39.616417+00:00',6,'2048230803edb6c0b1b926d5127da42f211f83a86160df77084f86d43f19dae2',1,'saved by hand',2,'8b00bf6a0ff60d2dd15fcf736139c8a9e9f63b47d8b98fa520685110b45d66aa');
CREATE UNIQUE INDEX idx_events_ref ON events(backend, ref) WHERE ref IS NOT NULL;
CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL;
CREATE INDEX idx_events_op ON events(op, seq);
CREATE INDEX idx_events_trust ON events(trust, seq);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('snapshots',2);
COMMIT;
