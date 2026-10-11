-- memdebug 0.6.1; user_version=3; events=7
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
INSERT INTO "events" VALUES(1,'e1','folder',NULL,NULL,'{"after":"has a dog called Rex\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"pets.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:40.001420Z","ts_observed":true}','0000000000000000000000000000000000000000000000000000000000000000','8d8cd6f861fadb66f9cbb72d6bd3285da9b99f714caf8912ac948fb608221c8c','ADD','unknown');
INSERT INTO "events" VALUES(2,'e2','folder',NULL,NULL,'{"after":"likes tea\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"tea.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:40.001420Z","ts_observed":true}','8d8cd6f861fadb66f9cbb72d6bd3285da9b99f714caf8912ac948fb608221c8c','f1305f0932048ee2eb4fc85410365d2e2afe3f3120e3f212f1938fd27ab90ed0','ADD','unknown');
INSERT INTO "events" VALUES(3,'e3','folder',NULL,'SNAPSHOT:s1','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"ff335377cbc4ed7b278e9bc49d0c1e89f7213af33f244f7ee96df5a2bd68e217\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s1","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:40.006239Z","ts_observed":false}','f1305f0932048ee2eb4fc85410365d2e2afe3f3120e3f212f1938fd27ab90ed0','6a8f461b8041078eaf2b71831292a30149cac0ffe789b3f761a88dfdce3fdd79','SNAPSHOT','unknown');
INSERT INTO "events" VALUES(4,'e4','folder',NULL,NULL,'{"after":"likes green tea\n","backend":"folder","backend_ref":null,"before":"likes tea\n","memory_id":"tea.md","op":"UPDATE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:40.579959Z","ts_observed":true}','6a8f461b8041078eaf2b71831292a30149cac0ffe789b3f761a88dfdce3fdd79','8c28ce4340a518919ccfcf35f40dc7bd78cbb0ef6a82cb847b92bc21f81b6c25','UPDATE','unknown');
INSERT INTO "events" VALUES(5,'e5','folder',NULL,NULL,'{"after":"waters the fern on Sundays\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"plants.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:40.859410Z","ts_observed":true}','8c28ce4340a518919ccfcf35f40dc7bd78cbb0ef6a82cb847b92bc21f81b6c25','c2cbb0307f833babc88fa676c164301ec049f46fa15708d8099474864d8fe3c0','ADD','unknown');
INSERT INTO "events" VALUES(6,'e6','folder',NULL,NULL,'{"after":null,"backend":"folder","backend_ref":null,"before":"has a dog called Rex\n","memory_id":"pets.md","op":"DELETE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:41.150498Z","ts_observed":true}','c2cbb0307f833babc88fa676c164301ec049f46fa15708d8099474864d8fe3c0','54fdbc21c7d14ca6889c2ce881502e997f5e4abd3f3c0a1ac9d6309038fe7e91','DELETE','unknown');
INSERT INTO "events" VALUES(7,'e7','folder',NULL,'SNAPSHOT:s2','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"f3bd8ff59369bc1ab2c59a0d7175f95aa0c2081e4ca50789eb86fdfb84ece560\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s2","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:41.444812Z","ts_observed":false}','54fdbc21c7d14ca6889c2ce881502e997f5e4abd3f3c0a1ac9d6309038fe7e91','169bcdf1b5d835696fec69203b6c379162c7e03c4098373e1396b651acab4e82','SNAPSHOT','unknown');
CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL, memory_id TEXT NOT NULL, hash TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY (snapshot_id, memory_id));
INSERT INTO "snapshot_entries" VALUES(1,'pets.md','84925127924d6bfb40f9d9a29f9bfe95c9027d2d72966e69c56e0f2bc06bc2de','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(1,'tea.md','e97db6a7402fda801f2e18ecb8b393ca05dc97d02963a8756541783224a3bbaf','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(2,'plants.md','1464212e01e44c6d1f7e1906fd7bf79304bba267b866c9c7fe8e5ce4c1907895','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(2,'tea.md','dbd001223420711d7ee2977345ae3c2bc090b2a320cfbedd72b8e11e83060966','{"store":"notes"}');
CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL, scope TEXT NOT NULL, taken_at TEXT NOT NULL, ledger_seq INTEGER NOT NULL, ledger_head TEXT NOT NULL, complete INTEGER NOT NULL, label TEXT, count INTEGER NOT NULL, entries_hash TEXT NOT NULL);
INSERT INTO "snapshots" VALUES(1,'folder','{"store":"notes"}','2026-10-11T08:43:40.006239+00:00',2,'f1305f0932048ee2eb4fc85410365d2e2afe3f3120e3f212f1938fd27ab90ed0',1,'baseline',2,'ff335377cbc4ed7b278e9bc49d0c1e89f7213af33f244f7ee96df5a2bd68e217');
INSERT INTO "snapshots" VALUES(2,'folder','{"store":"notes"}','2026-10-11T08:43:41.444812+00:00',6,'54fdbc21c7d14ca6889c2ce881502e997f5e4abd3f3c0a1ac9d6309038fe7e91',1,'saved by hand',2,'f3bd8ff59369bc1ab2c59a0d7175f95aa0c2081e4ca50789eb86fdfb84ece560');
CREATE UNIQUE INDEX idx_events_ref ON events(backend, ref) WHERE ref IS NOT NULL;
CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL;
CREATE INDEX idx_events_op ON events(op, seq);
CREATE INDEX idx_events_trust ON events(trust, seq);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('snapshots',2);
COMMIT;
