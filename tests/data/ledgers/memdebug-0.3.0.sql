-- memdebug 0.3.0; user_version=3; events=7
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
INSERT INTO "events" VALUES(1,'e1','folder',NULL,NULL,'{"after":"has a dog called Rex\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"pets.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:36.363827Z","ts_observed":true}','0000000000000000000000000000000000000000000000000000000000000000','6edfccdffc4b71324d4f11c7a82e399ab1d371e11646e490599fd82d36a7c610','ADD','unknown');
INSERT INTO "events" VALUES(2,'e2','folder',NULL,NULL,'{"after":"likes tea\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"tea.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:36.363827Z","ts_observed":true}','6edfccdffc4b71324d4f11c7a82e399ab1d371e11646e490599fd82d36a7c610','352f4ff0a6446776cebaae8a73ecb083535631dd2cd4f9dd5689a80559d339e7','ADD','unknown');
INSERT INTO "events" VALUES(3,'e3','folder',NULL,'SNAPSHOT:s1','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"68ce7eeefd38302b9526713b0bdc7b76fd05a775676f2556fed2a1852726ef8f\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s1","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:36.368782Z","ts_observed":false}','352f4ff0a6446776cebaae8a73ecb083535631dd2cd4f9dd5689a80559d339e7','3d4d12d5e959a284bd6e9141e44212585a1961d08f455d7456a12f477d10d114','SNAPSHOT','unknown');
INSERT INTO "events" VALUES(4,'e4','folder',NULL,NULL,'{"after":"likes green tea\n","backend":"folder","backend_ref":null,"before":"likes tea\n","memory_id":"tea.md","op":"UPDATE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:36.948147Z","ts_observed":true}','3d4d12d5e959a284bd6e9141e44212585a1961d08f455d7456a12f477d10d114','54f755bceb3f59228370240e072869ded2be30ecd483c987f06b35407202f52d','UPDATE','unknown');
INSERT INTO "events" VALUES(5,'e5','folder',NULL,NULL,'{"after":"waters the fern on Sundays\n","backend":"folder","backend_ref":null,"before":null,"memory_id":"plants.md","op":"ADD","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:37.226892Z","ts_observed":true}','54f755bceb3f59228370240e072869ded2be30ecd483c987f06b35407202f52d','5d5adcab04185bfe94acc22a20ec3e894c244aee1c93db8977e1c979a56ef668','ADD','unknown');
INSERT INTO "events" VALUES(6,'e6','folder',NULL,NULL,'{"after":null,"backend":"folder","backend_ref":null,"before":"has a dog called Rex\n","memory_id":"pets.md","op":"DELETE","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:37.507873Z","ts_observed":true}','5d5adcab04185bfe94acc22a20ec3e894c244aee1c93db8977e1c979a56ef668','940350e1ce380c724265bac3c7fc61693ecd45ad0dd1657aa3aa9a1191112248','DELETE','unknown');
INSERT INTO "events" VALUES(7,'e7','folder',NULL,'SNAPSHOT:s2','{"after":"{\"complete\":true,\"count\":2,\"hash\":\"4b5218543c567d5adc9b94cfec846ef856866140963acf64302f059217096479\"}","backend":"folder","backend_ref":null,"before":null,"memory_id":"snapshot:s2","op":"SNAPSHOT","scope":{"store":"notes"},"source":null,"trust":"unknown","ts":"2026-10-11T08:43:37.790981Z","ts_observed":false}','940350e1ce380c724265bac3c7fc61693ecd45ad0dd1657aa3aa9a1191112248','5157a4f19e79054d616b7bbda6d2d4fe148b901fce9dfca7455c861f59326d06','SNAPSHOT','unknown');
CREATE TABLE snapshot_entries (snapshot_id INTEGER NOT NULL, memory_id TEXT NOT NULL, hash TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY (snapshot_id, memory_id));
INSERT INTO "snapshot_entries" VALUES(1,'pets.md','84925127924d6bfb40f9d9a29f9bfe95c9027d2d72966e69c56e0f2bc06bc2de','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(1,'tea.md','e97db6a7402fda801f2e18ecb8b393ca05dc97d02963a8756541783224a3bbaf','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(2,'plants.md','1464212e01e44c6d1f7e1906fd7bf79304bba267b866c9c7fe8e5ce4c1907895','{"store":"notes"}');
INSERT INTO "snapshot_entries" VALUES(2,'tea.md','dbd001223420711d7ee2977345ae3c2bc090b2a320cfbedd72b8e11e83060966','{"store":"notes"}');
CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, backend TEXT NOT NULL, scope TEXT NOT NULL, taken_at TEXT NOT NULL, ledger_seq INTEGER NOT NULL, ledger_head TEXT NOT NULL, complete INTEGER NOT NULL, label TEXT, count INTEGER NOT NULL, entries_hash TEXT NOT NULL);
INSERT INTO "snapshots" VALUES(1,'folder','{"store":"notes"}','2026-10-11T08:43:36.368782+00:00',2,'352f4ff0a6446776cebaae8a73ecb083535631dd2cd4f9dd5689a80559d339e7',1,'baseline',2,'68ce7eeefd38302b9526713b0bdc7b76fd05a775676f2556fed2a1852726ef8f');
INSERT INTO "snapshots" VALUES(2,'folder','{"store":"notes"}','2026-10-11T08:43:37.790981+00:00',6,'940350e1ce380c724265bac3c7fc61693ecd45ad0dd1657aa3aa9a1191112248',1,'saved by hand',2,'4b5218543c567d5adc9b94cfec846ef856866140963acf64302f059217096479');
CREATE UNIQUE INDEX idx_events_ref ON events(backend, ref) WHERE ref IS NOT NULL;
CREATE UNIQUE INDEX idx_events_snap ON events(snap) WHERE snap IS NOT NULL;
CREATE INDEX idx_events_op ON events(op, seq);
CREATE INDEX idx_events_trust ON events(trust, seq);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('snapshots',2);
COMMIT;
