-- memdebug 0.2.0; user_version=3; events=6
BEGIN TRANSACTION;
CREATE TABLE blobs (hash TEXT PRIMARY KEY, text TEXT NOT NULL);
INSERT INTO "blobs" VALUES('84925127924d6bfb40f9d9a29f9bfe95c9027d2d72966e69c56e0f2bc06bc2de','has a dog called Rex
');
INSERT INTO "blobs" VALUES('e97db6a7402fda801f2e18ecb8b393ca05dc97d02963a8756541783224a3bbaf','likes tea
');
CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, backend TEXT NOT NULL, ref TEXT, snap TEXT, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, op TEXT, trust TEXT);
INSERT INTO "events" VALUES(1,'e1','folder',NULL,NULL,'{"after":"has a dog called Rex\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"pets.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:34.424876Z","ts_observed":true}','0000000000000000000000000000000000000000000000000000000000000000','37b24e74c4d9a61b494f0543441b78681608ec00f2c4e9ced085d6fccbed0d8d','ADD','unknown');
INSERT INTO "events" VALUES(2,'e2','folder',NULL,NULL,'{"after":"likes tea\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"tea.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:34.424876Z","ts_observed":true}','37b24e74c4d9a61b494f0543441b78681608ec00f2c4e9ced085d6fccbed0d8d','aa1320cecfa71b4b864c28e8c9846c8a8561ccfe46a93a1bbc00c37b01d43c4d','ADD','unknown');
INSERT INTO "events" VALUES(3,'e3','folder',NULL,'SNAPSHOT:s1','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"1bb7f53d6bdbf53604b3c0c1863d139ff63bc15178fdbabc5c9a8199e2488b92\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s1","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:34.438433Z","ts_observed":false}','aa1320cecfa71b4b864c28e8c9846c8a8561ccfe46a93a1bbc00c37b01d43c4d','4b75b8430e52184ad256ac5077a111074ac015d3032437e30b3fcf6f20e17ffd','SNAPSHOT','unknown');
INSERT INTO "events" VALUES(4,'e4','folder',NULL,NULL,'{"after":"likes green tea\n","backend":"folder","backend_ref":null,"before":"likes tea\n","memory_id":"tea.md","op":"UPDATE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:35.008560Z","ts_observed":true}','4b75b8430e52184ad256ac5077a111074ac015d3032437e30b3fcf6f20e17ffd','4b28a552e605139bd9408d3a6669c34297ee6a1ee1861219b74a5c485b4201ef','UPDATE','unknown');
INSERT INTO "events" VALUES(5,'e5','folder',NULL,NULL,'{"after":"waters the fern on Sundays\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"plants.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:35.290388Z","ts_observed":true}','4b28a552e605139bd9408d3a6669c34297ee6a1ee1861219b74a5c485b4201ef','880e90e6ab8a37b4dc9560aeacfb9f824b771cf5f88a207785fd18b4981e5118','ADD','unknown');
INSERT INTO "events" VALUES(6,'e6','folder',NULL,NULL,'{"after":null,"backend":"folder","backend_ref":null,"before":"has a dog called Rex\n","memory_id":"pets.md","op":"DELETE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:35.574781Z","ts_observed":true}','880e90e6ab8a37b4dc9560aeacfb9f824b771cf5f88a207785fd18b4981e5118','69e6a08343083e8e4e199bf53b7bdb0f051a1ba9180139452391d31f9130cd24','DELETE','unknown');
CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL, memory_id TEXT NOT NULL, hash TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY (snapshot_id, memory_id));
INSERT INTO "snapshot_entries" VALUES(1,'pets.md','84925127924d6bfb40f9d9a29f9bfe95c9027d2d72966e69c56e0f2bc06bc2de','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(1,'tea.md','e97db6a7402fda801f2e18ecb8b393ca05dc97d02963a8756541783224a3bbaf','{"store":"notes"}');
CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL, scope TEXT NOT NULL, taken_at TEXT NOT NULL, ledger_seq INTEGER NOT NULL, ledger_head TEXT NOT NULL, complete INTEGER NOT NULL, label TEXT, count INTEGER NOT NULL, entries_hash TEXT NOT NULL);
INSERT INTO "snapshots" VALUES(1,'folder','{"store":"notes"}','2026-10-11T08:43:34.438433+00:00',2,'aa1320cecfa71b4b864c28e8c9846c8a8561ccfe46a93a1bbc00c37b01d43c4d',1,'baseline',2,'1bb7f53d6bdbf53604b3c0c1863d139ff63bc15178fdbabc5c9a8199e2488b92');
CREATE UNIQUE INDEX idx_events_ref ON events(backend, ref) WHERE ref IS NOT NULL;
CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL;
CREATE INDEX idx_events_op ON events(op, seq);
CREATE INDEX idx_events_trust ON events(trust, seq);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('snapshots',1);
COMMIT;
