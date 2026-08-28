from __future__ import annotations

from pathlib import Path

from benchmarks.formal_eval.contracts import QueryContract, QueryOutputField
from benchmarks.formal_eval.semantic_failure import (
    DatabaseSpec,
    TaskSpec,
    build_semantic_failure_bundle,
)


DATASET_RELEASE = "semantic-join-contract-failure-v1"


def build_semantic_contract_failure_v1(
    sealed_root: Path,
    output: Path,
) -> dict[str, object]:
    return build_semantic_failure_bundle(
        sealed_root,
        output,
        dataset_release=DATASET_RELEASE,
        database_specs=_database_specs(),
        claim_boundary="trusted_query_contract_join_safety_stress_only",
    )


def _contract(
    required_entities: tuple[str, ...],
    row_grain_entity: str,
    *output_fields: tuple[str, str, str],
) -> QueryContract:
    return QueryContract(
        required_entities=required_entities,
        output_fields=tuple(
            QueryOutputField(entity=entity, column=column, alias=alias)
            for entity, column, alias in output_fields
        ),
        row_grain_entity=row_grain_entity,
    )


def _database_specs() -> tuple[DatabaseSpec, ...]:
    return (
        DatabaseSpec("logistics_v2", "logistics", _LOGISTICS_SQL, _LOGISTICS_TASKS),
        DatabaseSpec("education_v2", "education", _EDUCATION_SQL, _EDUCATION_TASKS),
        DatabaseSpec("media_v2", "media", _MEDIA_SQL, _MEDIA_TASKS),
        DatabaseSpec("insurance_v2", "insurance", _INSURANCE_SQL, _INSURANCE_TASKS),
    )


_LOGISTICS_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE depots (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE carriers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE shipments (id INTEGER PRIMARY KEY, customer_ref INTEGER NOT NULL REFERENCES customers(id), origin_ref INTEGER NOT NULL REFERENCES depots(id), carrier_ref INTEGER NOT NULL REFERENCES carriers(id));
CREATE TABLE shipment_items (id INTEGER PRIMARY KEY, shipment_ref INTEGER NOT NULL REFERENCES shipments(id), product_ref INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL);
CREATE TABLE delivery_events (id INTEGER PRIMARY KEY, shipment_ref INTEGER NOT NULL REFERENCES shipments(id), depot_ref INTEGER NOT NULL REFERENCES depots(id), event_type TEXT NOT NULL);
INSERT INTO customers VALUES (101,'Acme'),(202,'Beta'),(303,'Cedar');
INSERT INTO depots VALUES (101,'North'),(202,'Central'),(303,'South');
INSERT INTO carriers VALUES (101,'Swift'),(202,'ParcelCo'),(303,'Roadline');
INSERT INTO products VALUES (101,'Cable'),(202,'Display'),(303,'Keyboard');
INSERT INTO shipments VALUES (101,202,303,202),(202,303,101,303),(404,101,202,101);
INSERT INTO shipment_items VALUES (101,202,303,1),(202,101,202,2),(303,404,101,1),(404,101,303,3);
INSERT INTO delivery_events VALUES (101,202,101,'loaded'),(202,101,303,'sorted'),(303,404,202,'delivered');
"""

_LOGISTICS_TASKS = (
    TaskSpec(
        "shipment_customer",
        "Return the shipment identifier and the receiving customer name.",
        "SELECT s.id,c.name FROM shipments s JOIN customers c ON s.customer_ref=c.id ORDER BY s.id",
        "SELECT s.id,c.name FROM shipments s JOIN customers c ON s.id=c.id ORDER BY s.id",
        (("customers.id", "shipments.customer_ref"),),
        ("customers", "shipments"),
        "same_name_id",
        "one_to_many",
        _contract(
            ("customers", "shipments"),
            "shipments",
            ("shipments", "id", "shipment_id"),
            ("customers", "name", "customer_name"),
        ),
    ),
    TaskSpec(
        "item_product",
        "Return each shipment-item identifier and its product name.",
        "SELECT i.id,p.name FROM shipment_items i JOIN products p ON i.product_ref=p.id ORDER BY i.id",
        "SELECT i.id,p.name FROM shipment_items i JOIN products p ON i.id=p.id ORDER BY i.id",
        (("products.id", "shipment_items.product_ref"),),
        ("products", "shipment_items"),
        "integer_domain_collision",
        "one_to_many",
        _contract(
            ("products", "shipment_items"),
            "shipment_items",
            ("shipment_items", "id", "shipment_item_id"),
            ("products", "name", "product_name"),
        ),
    ),
    TaskSpec(
        "customer_product",
        "Return the customer name and product name for every shipment item.",
        "SELECT c.name,p.name FROM shipment_items i JOIN shipments s ON i.shipment_ref=s.id JOIN customers c ON s.customer_ref=c.id JOIN products p ON i.product_ref=p.id ORDER BY i.id",
        "SELECT c.name,p.name FROM shipment_items i JOIN shipments s ON i.shipment_ref=s.id JOIN customers c ON s.id=c.id JOIN products p ON i.product_ref=p.id ORDER BY i.id",
        (("customers.id", "shipments.customer_ref"), ("products.id", "shipment_items.product_ref"), ("shipment_items.shipment_ref", "shipments.id")),
        ("customers", "products", "shipment_items", "shipments"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("customers", "products", "shipment_items", "shipments"),
            "shipment_items",
            ("customers", "name", "customer_name"),
            ("products", "name", "product_name"),
        ),
    ),
    TaskSpec(
        "shipment_origin",
        "Return each shipment identifier and its origin depot name.",
        "SELECT s.id,d.name FROM shipments s JOIN depots d ON s.origin_ref=d.id ORDER BY s.id",
        "SELECT s.id,d.name FROM shipments s JOIN depots d ON s.customer_ref=d.id ORDER BY s.id",
        (("depots.id", "shipments.origin_ref"),),
        ("depots", "shipments"),
        "multiple_parents",
        "one_to_many",
        _contract(
            ("depots", "shipments"),
            "shipments",
            ("shipments", "id", "shipment_id"),
            ("depots", "name", "origin_depot_name"),
        ),
    ),
    TaskSpec(
        "event_carrier",
        "Return each delivery-event identifier and the shipment carrier name.",
        "SELECT e.id,c.name FROM delivery_events e JOIN shipments s ON e.shipment_ref=s.id JOIN carriers c ON s.carrier_ref=c.id ORDER BY e.id",
        "SELECT e.id,c.name FROM delivery_events e JOIN shipments s ON e.shipment_ref=s.id JOIN carriers c ON e.depot_ref=c.id ORDER BY e.id",
        (("carriers.id", "shipments.carrier_ref"), ("delivery_events.shipment_ref", "shipments.id")),
        ("carriers", "delivery_events", "shipments"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("carriers", "delivery_events", "shipments"),
            "delivery_events",
            ("delivery_events", "id", "delivery_event_id"),
            ("carriers", "name", "carrier_name"),
        ),
    ),
)


_EDUCATION_SQL = """
CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE instructors (id INTEGER PRIMARY KEY, department_ref INTEGER NOT NULL REFERENCES departments(id), name TEXT NOT NULL);
CREATE TABLE courses (id INTEGER PRIMARY KEY, instructor_ref INTEGER NOT NULL REFERENCES instructors(id), name TEXT NOT NULL);
CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE enrollments (id INTEGER PRIMARY KEY, student_ref INTEGER NOT NULL REFERENCES students(id), course_ref INTEGER NOT NULL REFERENCES courses(id));
CREATE TABLE classrooms (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE course_sessions (id INTEGER PRIMARY KEY, course_ref INTEGER NOT NULL REFERENCES courses(id), classroom_ref INTEGER NOT NULL REFERENCES classrooms(id));
INSERT INTO departments VALUES (11,'Science'),(22,'Arts'),(33,'Business');
INSERT INTO instructors VALUES (11,22,'Inez'),(22,33,'Mora'),(44,11,'Niko');
INSERT INTO courses VALUES (11,22,'Statistics'),(22,44,'Design'),(33,11,'Finance');
INSERT INTO students VALUES (11,'Ari'),(22,'Bo'),(33,'Cy');
INSERT INTO enrollments VALUES (11,22,33),(22,33,11),(33,11,22);
INSERT INTO classrooms VALUES (11,'R1'),(22,'R2'),(33,'R3');
INSERT INTO course_sessions VALUES (11,22,33),(22,33,11),(33,11,22);
"""

_EDUCATION_TASKS = (
    TaskSpec(
        "enrollment_student",
        "Return each enrollment identifier and its student name.",
        "SELECT e.id,s.name FROM enrollments e JOIN students s ON e.student_ref=s.id ORDER BY e.id",
        "SELECT e.id,s.name FROM enrollments e JOIN students s ON e.id=s.id ORDER BY e.id",
        (("enrollments.student_ref", "students.id"),),
        ("enrollments", "students"),
        "same_name_id",
        "one_to_many",
        _contract(
            ("enrollments", "students"),
            "enrollments",
            ("enrollments", "id", "enrollment_id"),
            ("students", "name", "student_name"),
        ),
    ),
    TaskSpec(
        "course_instructor",
        "Return each course name and its instructor name.",
        "SELECT c.name,i.name FROM courses c JOIN instructors i ON c.instructor_ref=i.id ORDER BY c.id",
        "SELECT c.name,i.name FROM courses c JOIN instructors i ON c.id=i.id ORDER BY c.id",
        (("courses.instructor_ref", "instructors.id"),),
        ("courses", "instructors"),
        "integer_domain_collision",
        "one_to_many",
        _contract(
            ("courses", "instructors"),
            "courses",
            ("courses", "name", "course_name"),
            ("instructors", "name", "instructor_name"),
        ),
    ),
    TaskSpec(
        "student_department",
        "Return the student name and teaching department name for every enrollment.",
        "SELECT s.name,d.name FROM enrollments e JOIN students s ON e.student_ref=s.id JOIN courses c ON e.course_ref=c.id JOIN instructors i ON c.instructor_ref=i.id JOIN departments d ON i.department_ref=d.id ORDER BY e.id",
        "SELECT s.name,d.name FROM enrollments e JOIN students s ON e.student_ref=s.id JOIN courses c ON e.course_ref=c.id JOIN instructors i ON c.id=i.id JOIN departments d ON i.department_ref=d.id ORDER BY e.id",
        (("courses.id", "enrollments.course_ref"), ("courses.instructor_ref", "instructors.id"), ("departments.id", "instructors.department_ref"), ("enrollments.student_ref", "students.id")),
        ("courses", "departments", "enrollments", "instructors", "students"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("courses", "departments", "enrollments", "instructors", "students"),
            "enrollments",
            ("students", "name", "student_name"),
            ("departments", "name", "department_name"),
        ),
    ),
    TaskSpec(
        "course_classroom",
        "Return the course name and classroom name for every course session.",
        "SELECT c.name,r.name FROM course_sessions s JOIN courses c ON s.course_ref=c.id JOIN classrooms r ON s.classroom_ref=r.id ORDER BY s.id",
        "SELECT c.name,r.name FROM course_sessions s JOIN courses c ON s.id=c.id JOIN classrooms r ON s.course_ref=r.id ORDER BY s.id",
        (("classrooms.id", "course_sessions.classroom_ref"), ("course_sessions.course_ref", "courses.id")),
        ("classrooms", "course_sessions", "courses"),
        "integer_domain_collision",
        "one_to_many",
        _contract(
            ("classrooms", "course_sessions", "courses"),
            "course_sessions",
            ("courses", "name", "course_name"),
            ("classrooms", "name", "classroom_name"),
        ),
    ),
    TaskSpec(
        "department_course",
        "Return each course name and the instructor department name.",
        "SELECT c.name,d.name FROM courses c JOIN instructors i ON c.instructor_ref=i.id JOIN departments d ON i.department_ref=d.id ORDER BY c.id",
        "SELECT c.name,d.name FROM courses c JOIN instructors i ON c.id=i.id JOIN departments d ON i.department_ref=d.id ORDER BY c.id",
        (("courses.instructor_ref", "instructors.id"), ("departments.id", "instructors.department_ref")),
        ("courses", "departments", "instructors"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("courses", "departments", "instructors"),
            "courses",
            ("courses", "name", "course_name"),
            ("departments", "name", "department_name"),
        ),
    ),
)


_MEDIA_SQL = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_ref INTEGER NOT NULL REFERENCES artists(id), title TEXT NOT NULL);
CREATE TABLE tracks (id INTEGER PRIMARY KEY, album_ref INTEGER NOT NULL REFERENCES albums(id), title TEXT NOT NULL);
CREATE TABLE playlists (id INTEGER PRIMARY KEY, owner_ref INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL);
CREATE TABLE playlist_items (id INTEGER PRIMARY KEY, playlist_ref INTEGER NOT NULL REFERENCES playlists(id), track_ref INTEGER NOT NULL REFERENCES tracks(id));
CREATE TABLE plays (id INTEGER PRIMARY KEY, listener_ref INTEGER NOT NULL REFERENCES users(id), track_ref INTEGER NOT NULL REFERENCES tracks(id));
INSERT INTO users VALUES (7,'Uma'),(14,'Vic'),(28,'Wes');
INSERT INTO artists VALUES (7,'Aster'),(14,'Beryl'),(28,'Coda');
INSERT INTO albums VALUES (7,14,'Northbound'),(14,28,'Signal'),(35,7,'Tides');
INSERT INTO tracks VALUES (7,35,'Blue'),(14,7,'Gold'),(28,14,'Red');
INSERT INTO playlists VALUES (7,28,'Focus'),(14,7,'Drive'),(35,14,'Quiet');
INSERT INTO playlist_items VALUES (7,14,28),(14,35,7),(28,7,14);
INSERT INTO plays VALUES (7,14,28),(14,28,7),(28,7,14);
"""

_MEDIA_TASKS = (
    TaskSpec(
        "album_artist",
        "Return each album title and its artist name.",
        "SELECT a.title,r.name FROM albums a JOIN artists r ON a.artist_ref=r.id ORDER BY a.id",
        "SELECT a.title,r.name FROM albums a JOIN artists r ON a.id=r.id ORDER BY a.id",
        (("albums.artist_ref", "artists.id"),),
        ("albums", "artists"),
        "same_name_id",
        "one_to_many",
        _contract(
            ("albums", "artists"),
            "albums",
            ("albums", "title", "album_title"),
            ("artists", "name", "artist_name"),
        ),
    ),
    TaskSpec(
        "track_artist",
        "Return each track title and its album artist name.",
        "SELECT t.title,r.name FROM tracks t JOIN albums a ON t.album_ref=a.id JOIN artists r ON a.artist_ref=r.id ORDER BY t.id",
        "SELECT t.title,r.name FROM tracks t JOIN albums a ON t.id=a.id JOIN artists r ON a.artist_ref=r.id ORDER BY t.id",
        (("albums.artist_ref", "artists.id"), ("albums.id", "tracks.album_ref")),
        ("albums", "artists", "tracks"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("albums", "artists", "tracks"),
            "tracks",
            ("tracks", "title", "track_title"),
            ("artists", "name", "artist_name"),
        ),
    ),
    TaskSpec(
        "playlist_owner",
        "Return each playlist title and its owner name.",
        "SELECT p.title,u.name FROM playlists p JOIN users u ON p.owner_ref=u.id ORDER BY p.id",
        "SELECT p.title,u.name FROM playlists p JOIN users u ON p.id=u.id ORDER BY p.id",
        (("playlists.owner_ref", "users.id"),),
        ("playlists", "users"),
        "integer_domain_collision",
        "one_to_many",
        _contract(
            ("playlists", "users"),
            "playlists",
            ("playlists", "title", "playlist_title"),
            ("users", "name", "owner_name"),
        ),
    ),
    TaskSpec(
        "playlist_track",
        "Return the playlist title and track title for every playlist item.",
        "SELECT p.title,t.title FROM playlist_items i JOIN playlists p ON i.playlist_ref=p.id JOIN tracks t ON i.track_ref=t.id ORDER BY i.id",
        "SELECT p.title,t.title FROM playlist_items i JOIN playlists p ON i.id=p.id JOIN tracks t ON i.playlist_ref=t.id ORDER BY i.id",
        (("playlist_items.playlist_ref", "playlists.id"), ("playlist_items.track_ref", "tracks.id")),
        ("playlist_items", "playlists", "tracks"),
        "integer_domain_collision",
        "one_to_many",
        _contract(
            ("playlist_items", "playlists", "tracks"),
            "playlist_items",
            ("playlists", "title", "playlist_title"),
            ("tracks", "title", "track_title"),
        ),
    ),
    TaskSpec(
        "listener_artist",
        "Return the listener name and artist name for every play.",
        "SELECT u.name,r.name FROM plays p JOIN users u ON p.listener_ref=u.id JOIN tracks t ON p.track_ref=t.id JOIN albums a ON t.album_ref=a.id JOIN artists r ON a.artist_ref=r.id ORDER BY p.id",
        "SELECT u.name,r.name FROM plays p JOIN users u ON p.id=u.id JOIN tracks t ON p.track_ref=t.id JOIN albums a ON t.album_ref=a.id JOIN artists r ON a.artist_ref=r.id ORDER BY p.id",
        (("albums.artist_ref", "artists.id"), ("albums.id", "tracks.album_ref"), ("plays.listener_ref", "users.id"), ("plays.track_ref", "tracks.id")),
        ("albums", "artists", "plays", "tracks", "users"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("albums", "artists", "plays", "tracks", "users"),
            "plays",
            ("users", "name", "listener_name"),
            ("artists", "name", "artist_name"),
        ),
    ),
)


_INSURANCE_SQL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE policies (id INTEGER PRIMARY KEY, holder_ref INTEGER NOT NULL REFERENCES customers(id), policy_number TEXT NOT NULL);
CREATE TABLE adjusters (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE claims (id INTEGER PRIMARY KEY, policy_ref INTEGER NOT NULL REFERENCES policies(id), handler_ref INTEGER NOT NULL REFERENCES adjusters(id), claim_number TEXT NOT NULL);
CREATE TABLE claim_notes (id INTEGER PRIMARY KEY, claim_ref INTEGER NOT NULL REFERENCES claims(id), body TEXT NOT NULL);
CREATE TABLE coverages (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE policy_coverages (id INTEGER PRIMARY KEY, policy_ref INTEGER NOT NULL REFERENCES policies(id), coverage_ref INTEGER NOT NULL REFERENCES coverages(id));
INSERT INTO customers VALUES (5,'Delta'),(15,'Elm'),(25,'Fjord');
INSERT INTO policies VALUES (5,15,'P-1'),(15,25,'P-2'),(35,5,'P-3');
INSERT INTO adjusters VALUES (5,'Gail'),(15,'Hugo'),(25,'Imani');
INSERT INTO claims VALUES (5,15,25,'C-1'),(15,35,5,'C-2'),(25,5,15,'C-3');
INSERT INTO claim_notes VALUES (5,15,'review'),(15,25,'contact'),(25,5,'closed');
INSERT INTO coverages VALUES (5,'Fire'),(15,'Flood'),(25,'Theft');
INSERT INTO policy_coverages VALUES (5,15,25),(15,35,5),(25,5,15);
"""

_INSURANCE_TASKS = (
    TaskSpec(
        "policy_customer",
        "Return each policy number and its holder name.",
        "SELECT p.policy_number,c.name FROM policies p JOIN customers c ON p.holder_ref=c.id ORDER BY p.id",
        "SELECT p.policy_number,c.name FROM policies p JOIN customers c ON p.id=c.id ORDER BY p.id",
        (("customers.id", "policies.holder_ref"),),
        ("customers", "policies"),
        "same_name_id",
        "one_to_many",
        _contract(
            ("customers", "policies"),
            "policies",
            ("policies", "policy_number", "policy_number"),
            ("customers", "name", "holder_name"),
        ),
    ),
    TaskSpec(
        "claim_customer",
        "Return each claim number and the policy-holder name.",
        "SELECT c.claim_number,u.name FROM claims c JOIN policies p ON c.policy_ref=p.id JOIN customers u ON p.holder_ref=u.id ORDER BY c.id",
        "SELECT c.claim_number,u.name FROM claims c JOIN policies p ON c.id=p.id JOIN customers u ON p.holder_ref=u.id ORDER BY c.id",
        (("claims.policy_ref", "policies.id"), ("customers.id", "policies.holder_ref")),
        ("claims", "customers", "policies"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("claims", "customers", "policies"),
            "claims",
            ("claims", "claim_number", "claim_number"),
            ("customers", "name", "holder_name"),
        ),
    ),
    TaskSpec(
        "claim_handler",
        "Return each claim number and its handler name.",
        "SELECT c.claim_number,a.name FROM claims c JOIN adjusters a ON c.handler_ref=a.id ORDER BY c.id",
        "SELECT c.claim_number,a.name FROM claims c JOIN adjusters a ON c.id=a.id ORDER BY c.id",
        (("adjusters.id", "claims.handler_ref"),),
        ("adjusters", "claims"),
        "integer_domain_collision",
        "one_to_many",
        _contract(
            ("adjusters", "claims"),
            "claims",
            ("claims", "claim_number", "claim_number"),
            ("adjusters", "name", "handler_name"),
        ),
    ),
    TaskSpec(
        "note_handler",
        "Return each claim-note identifier and the related claim handler name.",
        "SELECT n.id,a.name FROM claim_notes n JOIN claims c ON n.claim_ref=c.id JOIN adjusters a ON c.handler_ref=a.id ORDER BY n.id",
        "SELECT n.id,a.name FROM claim_notes n JOIN claims c ON n.id=c.id JOIN adjusters a ON c.handler_ref=a.id ORDER BY n.id",
        (("adjusters.id", "claims.handler_ref"), ("claim_notes.claim_ref", "claims.id")),
        ("adjusters", "claim_notes", "claims"),
        "integer_domain_collision",
        "compound",
        _contract(
            ("adjusters", "claim_notes", "claims"),
            "claim_notes",
            ("claim_notes", "id", "claim_note_id"),
            ("adjusters", "name", "handler_name"),
        ),
    ),
    TaskSpec(
        "customer_coverage",
        "Return the policy-holder name and coverage name for every policy coverage.",
        "SELECT c.name,v.name FROM policy_coverages x JOIN policies p ON x.policy_ref=p.id JOIN customers c ON p.holder_ref=c.id JOIN coverages v ON x.coverage_ref=v.id ORDER BY x.id",
        "SELECT c.name,v.name FROM policy_coverages x JOIN policies p ON x.id=p.id JOIN customers c ON p.holder_ref=c.id JOIN coverages v ON x.policy_ref=v.id ORDER BY x.id",
        (("coverages.id", "policy_coverages.coverage_ref"), ("customers.id", "policies.holder_ref"), ("policies.id", "policy_coverages.policy_ref")),
        ("coverages", "customers", "policies", "policy_coverages"),
        "wrong_hierarchy",
        "compound",
        _contract(
            ("coverages", "customers", "policies", "policy_coverages"),
            "policy_coverages",
            ("customers", "name", "holder_name"),
            ("coverages", "name", "coverage_name"),
        ),
    ),
)
