-- ============================================================
-- EagleGIS serving schema v3 — item-centric, category-first, RAG-ready
--
-- Replaces both the legacy Supabase tier (projects/meetings/documents/
-- locations) and the v2 bolt-on (meetings_v2/agenda_items/locations_v2/
-- documents_v2/agenda_embeddings). Mirrors data/silver/core/ 1:1 so
-- publishing is a dumb upsert.
--
-- Conventions:
--   * Pipeline-owned tables use pipeline-generated stable PKs (no serials)
--     -> re-publishing is idempotent, no ID drift, no reset machinery.
--   * CHECK constraints instead of Postgres ENUMs (easier to evolve).
--   * Extensions: vector (pgvector) required; postgis optional.
--
-- Publish order (parents first):
--   boards, meeting_formats, meeting_types, agenda_categories, projects,
--   locations -> meetings -> documents, agenda_items ->
--   agenda_item_locations, agenda_item_projects, motions -> rag_chunks.
--   All inserts as: INSERT ... ON CONFLICT (pk) DO UPDATE.
--
-- Stays OUT of the serving DB on purpose:
--   * data/silver/review/* (pipeline QA artifacts, incl. the
--     hand-curated geocoded_locations.csv input)
--   * meetings raw_text (full PDF text bloats the DB; rag_chunks carries
--     what retrieval needs, pdf_url points at the source PDF)
--   * legacy_locations.csv (dead duplicate of locations.csv)
-- ============================================================

create extension if not exists vector;
-- create extension if not exists postgis;   -- optional, enables geo queries

-- ---------- Reference tables (small, stable) ----------

create table public.boards (
  board_id     smallint primary key,
  code         varchar not null unique,          -- 'VC', 'PZDB'
  name         varchar not null,
  active_from  date,
  active_to    date
);

create table public.meeting_formats (
  format_id    smallint primary key,
  name         varchar not null unique,          -- 'Regular', 'Workshop', ...
  description  text
);

create table public.meeting_types (
  type_id      smallint primary key,
  type_name    varchar not null unique,
  description  text
);

-- The approved 6+2 taxonomy. map_role encodes the plotting rule:
--   primary_layer -> one of the 6 public map layers
--   conditional   -> Budget, Contracts & Purchasing (plotted only when
--                    tied to a specific place)
--   search_only   -> Meetings, Records & Public Input
create table public.agenda_categories (
  category_id    smallint primary key,
  category_name  varchar not null unique,
  description    text,
  map_role       varchar not null default 'primary_layer'
                 check (map_role in ('primary_layer','conditional','search_only'))
);

create table public.projects (
  project_id    integer primary key,
  project_name  varchar not null unique,
  description   text,
  status        varchar
);

-- ---------- Canonical locations (reusable, typed) ----------

create table public.locations (
  location_id         integer primary key,
  location_name       varchar not null,
  location_type       varchar check (location_type in
                        ('PARCEL','MULTI_PARCEL','INTERSECTION','CORRIDOR',
                         'WHOLE_STREET','NAMED_VENUE','NEIGHBORHOOD',
                         'ANCHORED_OFFSET','VENUE')),
  address             text,
  description         text,
  latitude            numeric(9,6),
  longitude           numeric(9,6),
  parcel_id           text,                      -- Lee County STRAP when resolved to a parcel
  geocode_source      varchar,                   -- 'resolver', 'manual_override', ...
  geocode_confidence  numeric(3,2)
  -- With postgis, uncomment for spatial queries ("items within 1 mi of X"):
  -- , geom geography(point,4326) generated always as
  --     (st_setsrid(st_makepoint(longitude::float8, latitude::float8),4326)::geography) stored
);
-- create index locations_geom_idx on public.locations using gist (geom);

-- ---------- Meetings & documents ----------

create table public.meetings (
  meeting_id         integer primary key,
  board_id           smallint not null references public.boards(board_id),
  format_id          smallint references public.meeting_formats(format_id),
  type_id            smallint references public.meeting_types(type_id),
  title              text,
  meeting_date       date not null,
  meeting_year       integer generated always as (extract(year from meeting_date)::int) stored,
  meeting_time       varchar,
  start_time         varchar,
  end_time           varchar,
  venue_location_id  integer references public.locations(location_id),
  venue_name         text,
  venue_address      text,
  pdf_url            text,                        -- estero-fl.gov minutes link
  summary            text,
  status             varchar,                     -- 'Held', 'Cancelled', ...
  filename           varchar,                     -- source PDF name (provenance)
  notes              text
);
create index meetings_date_idx  on public.meetings (meeting_date);
create index meetings_board_idx on public.meetings (board_id, meeting_date);

create table public.documents (
  document_id    integer primary key,
  meeting_id     integer not null references public.meetings(meeting_id),
  title          varchar not null,
  document_type  varchar check (document_type in
                   ('Agenda','Staff Report','Resolution','Ordinance','Minutes',
                    'Presentation','Public Notice','Packet')),
  file_name      varchar,
  file_url       text,
  doc_date       date,
  upload_date    date,
  notes          text
);
create index documents_meeting_idx on public.documents (meeting_id);

-- ---------- Agenda items (the atomic unit of the pipeline) ----------

create table public.agenda_items (
  item_id                integer primary key,
  meeting_id             integer not null references public.meetings(meeting_id),
  category_id            smallint not null references public.agenda_categories(category_id),
  item_number            varchar,
  item_order             integer,
  item_type              varchar,
  item_title             text,
  application_id         varchar,                 -- e.g. 'DCI2023-...'
  applicant_name         text,
  project_title          text,
  district               smallint,
  address_raw            text,
  summary                text,
  item_text              text,                    -- extracted source text; canonical RAG chunk source
  action_taken           text,
  action_type            varchar,
  outcome                text,
  motion_text            text,                    -- denormalized convenience copy; motions has detail
  vote_result            text,
  vote_detected          boolean,
  staff_code             varchar,
  needs_review           boolean default false,
  extraction_confidence  numeric(3,2),
  extraction_notes       text
  -- Optional (recommended once the pipeline can emit it): a normalized
  -- decision so the chatbot's "status" field is data, not LLM inference:
  -- , decision_status varchar check (decision_status in
  --     ('Approved','Denied','Continued','No decision recorded'))
);
create index agenda_items_meeting_idx  on public.agenda_items (meeting_id);
create index agenda_items_category_idx on public.agenda_items (category_id);

create table public.agenda_item_locations (
  item_id       integer not null references public.agenda_items(item_id),
  location_id   integer not null references public.locations(location_id),
  location_seq  smallint not null default 1,      -- LocationSeq in the map CSV
  is_primary    boolean  not null default true,   -- IsPrimary in the map CSV
  primary key (item_id, location_id)
);

create table public.agenda_item_projects (
  item_id     integer not null references public.agenda_items(item_id),
  project_id  integer not null references public.projects(project_id),
  primary key (item_id, project_id)
);

create table public.motions (
  motion_id     integer primary key,
  item_id       integer not null references public.agenda_items(item_id),
  motion_text   text,
  proposed_by   text,
  seconded_by   text,
  outcome       text,
  vote_yes      smallint,
  vote_no       smallint,
  vote_abstain  smallint
);

-- ---------- RAG layer (replaces agenda_embeddings) ----------
-- One row per retrievable chunk. Agenda items are naturally chunk-sized
-- (title + summary + action + motion ~ 100-400 tokens -> usually 1 chunk
-- per item); meeting summaries get their own chunks for "what happened at
-- the March meeting" questions. Covers ALL agenda items, including the
-- search_only categories that never appear on the map.
--
-- Filter columns (category_id, board_code, meeting_date, coordinates,
-- document_link) are DENORMALIZED on purpose: pgvector ANN + WHERE on the
-- same table avoids post-filter recall loss and gives the chatbot citation
-- fields without a join.
--
-- chunk_text format: keep the backend-compatible "Field: value" lines with
-- the exact field names the chat prompt already parses (ProjectName,
-- ApplicationID, Location/LocationName, MeetingDate, ActionTaken, Outcome,
-- Status, Document_Link), preceded by a keyword-rich "SEARCH:" header —
-- this matches what backend/app.py::enrich_doc builds at index time today.
--
-- vector(384) matches the backend's embedding model
-- (sentence-transformers/all-MiniLM-L6-v2, normalized -> cosine ops).
-- Change the dimension if the model changes.

create table public.rag_chunks (
  chunk_id       bigint generated always as identity primary key,  -- DB-owned: chunks are rebuilt, not tracked
  item_id        integer references public.agenda_items(item_id) on delete cascade,
  meeting_id     integer references public.meetings(meeting_id) on delete cascade,
  source_type    varchar not null check (source_type in
                   ('agenda_item','meeting_summary','motion')),
  chunk_index    smallint not null default 0,
  chunk_text     text not null,
  content_hash   text not null unique,            -- sha256 of chunk_text: publisher skips unchanged rows -> no re-embedding
  embedding      vector(384),
  fts            tsvector generated always as (to_tsvector('english', chunk_text)) stored,
  -- denormalized retrieval filters / citation payload:
  category_id    smallint,
  board_code     varchar,
  meeting_date   date,
  is_mapped      boolean default false,
  latitude       numeric(9,6),
  longitude      numeric(9,6),
  document_link  text
);
create index rag_chunks_embedding_idx on public.rag_chunks
  using hnsw (embedding vector_cosine_ops);
create index rag_chunks_fts_idx    on public.rag_chunks using gin (fts);
create index rag_chunks_filter_idx on public.rag_chunks (category_id, meeting_date);

-- ---------- Map view (mirrors arcgis_agenda_map_data + the 8 layer CSVs) ----------
-- Column-for-column reproduction of the pipeline's 36-column map format
-- (matches build.py's arcgis_agenda_map_data writer, incl. the
-- ProjectName-vs-ProjectTitle distinction, ArcGIS_Date, ProposedBy/
-- SecondedBy from the first motion, and the constant RecordType).
--
-- The pipeline still writes data/gold/arcgis/layers/*.csv as before —
-- this view replaces the 8 files only on the DATABASE side. Each ArcGIS
-- layer is: select * from v_map_items where category_id = N, and the same
-- per-category CSV exports can be regenerated from it at any time.
-- Column set is a superset of backend/data/data.csv, so an export of this
-- view is a drop-in replacement for the chatbot's current corpus.

create or replace view public.v_map_items as
select
  coalesce(p.project_name, ai.project_title) as project_name,   -- build.py: project link, falling back to item title
  ac.category_name            as layer_category,
  ac.category_id,
  b.name                      as board,
  mf.name                     as meeting_format,
  mt.type_name                as meeting_type,
  m.meeting_date,
  m.meeting_date              as arcgis_date,
  m.meeting_year,
  m.status,
  ai.item_id                  as agenda_item_id,
  ai.item_number              as agenda_item_number,
  ai.item_type                as agenda_item_type,
  ai.project_title,
  ai.summary,
  ai.action_taken,
  ai.outcome,
  ai.motion_text,
  mo.proposed_by,
  mo.seconded_by,
  ai.vote_result,
  ai.applicant_name,
  ai.application_id,
  ai.district,
  l.location_name,
  l.address                   as location,
  l.latitude,
  l.longitude,
  l.geocode_confidence,
  ai.staff_code,
  m.filename,
  m.pdf_url                   as document_link,
  'AgendaItemLocation'        as record_type,
  ail.location_seq,
  ail.is_primary,
  l.parcel_id
from public.agenda_items ai
join public.meetings              m   on m.meeting_id = ai.meeting_id
join public.agenda_categories     ac  on ac.category_id = ai.category_id
join public.boards                b   on b.board_id = m.board_id
left join public.meeting_formats  mf  on mf.format_id = m.format_id
left join public.meeting_types    mt  on mt.type_id = m.type_id
join public.agenda_item_locations ail on ail.item_id = ai.item_id
join public.locations             l   on l.location_id = ail.location_id
-- lateral limit-1 joins: pick one project / one motion per item so items
-- with several linked projects or motions don't fan out into extra rows
left join lateral (
  select pr.project_name
  from public.agenda_item_projects aip
  join public.projects pr on pr.project_id = aip.project_id
  where aip.item_id = ai.item_id
  order by pr.project_id
  limit 1
) p on true
left join lateral (
  select mo2.proposed_by, mo2.seconded_by
  from public.motions mo2
  where mo2.item_id = ai.item_id
  order by mo2.motion_id
  limit 1
) mo on true
where l.latitude is not null
  and ac.map_role in ('primary_layer','conditional');
