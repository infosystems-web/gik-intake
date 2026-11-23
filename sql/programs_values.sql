-- Programs and book values. Run in the Supabase SQL editor (project tep-teacher-platform) after distribution.sql.
-- Safe to re-run.
--
-- Programs: one shared catalog (item_master, products, offers) branches into programs, each with its own donations,
-- stock, pickups and reports. A scan belongs to a program through its donation; a scan-out through its distribution.
-- Book values: books with no store price (mostly used) get a standard thrift value from gik.value_guide, so every
-- book has a value. The amounts are a TEP estimate from published thrift guides until finance approves them.

-- ---------------------------------------------------------------- programs
create table if not exists gik.programs (
  program_code text primary key,
  name         text not null,
  description  text,
  active       boolean not null default true,
  sort_order   int not null default 100,
  created_at   timestamptz not null default now()
);
insert into gik.programs (program_code, name, description, sort_order) values
  ('TEP-GIK',   'TEP general GIK intake', 'Everything TEP receives for its own use and NetSuite', 10),
  ('TEP-BOOKS', 'TEP book distribution',  'Books sorted by grade and subject and scanned out to partner nonprofits', 20),
  ('NERC-HUB',  'Hub-and-spoke (NERC)',   'TEP as the donation hub sending GIK to partner organizations', 30),
  ('WYBAR',     'WYBAR',                  'WYBAR, Pittsburgh', 40)
on conflict (program_code) do nothing;

alter table gik.donations add column if not exists program_code text references gik.programs(program_code);
update gik.donations set program_code = 'TEP-GIK' where program_code is null;
alter table gik.donations alter column program_code set default 'TEP-GIK';
alter table gik.donations alter column program_code set not null;

alter table gik.distributions add column if not exists program_code text references gik.programs(program_code);
update gik.distributions set program_code = 'TEP-GIK' where program_code is null;  -- same program as existing donations
alter table gik.distributions alter column program_code set default 'TEP-GIK';
alter table gik.distributions alter column program_code set not null;

-- ---------------------------------------------------------------- book value guide (thrift values for used books)
create table if not exists gik.value_guide (
  guide_code  text primary key,
  label       text not null,
  book_format text not null,                       -- hardcover | paperback | children | other
  condition   text not null default 'used',
  unit_value  numeric(10,2) not null check (unit_value >= 0),
  basis       text not null,
  sources     text,
  status      text not null default 'TEP estimate' check (status in ('TEP estimate', 'approved')),
  approved_by text,
  approved_at timestamptz,
  updated_at  timestamptz not null default now()
);
insert into gik.value_guide (guide_code, label, book_format, unit_value, basis, sources) values
  ('BOOK-HARDCOVER-USED', 'Hardcover book, used', 'hardcover', 2.00,
   'Thrift value (IRS Pub. 561: used goods at what they sell for in used condition)',
   'Salvation Army guide $1-$3 (Bankrate 2025-26 list); Goodwill Greater Mid-Columbia 2023 $1.99'),
  ('BOOK-PAPERBACK-USED', 'Paperback book, used', 'paperback', 1.50,
   'Thrift value (IRS Pub. 561: used goods at what they sell for in used condition)',
   'Salvation Army guide $1-$2 (Bankrate 2025-26 list); Agrace 2020 $0.75-$3'),
  ('BOOK-CHILDREN-USED', 'Children''s picture or board book, used', 'children', 0.75,
   'Thrift value (IRS Pub. 561: used goods at what they sell for in used condition)',
   'Goodwill Greater Mid-Columbia 2023 $0.59; Agrace 2020 $0.50-$1'),
  ('BOOK-OTHER-USED', 'Book, binding unknown, used', 'other', 1.00,
   'Thrift value (IRS Pub. 561); low end of the paperback range when the binding is not known',
   'Salvation Army guide paperback $1-$2')
on conflict (guide_code) do nothing;

-- Format of a book from what we know: the Open Library line ("32 pages, Paperback"), the title and the sorting bin.
create or replace function gik.book_format(p_quantity text, p_name text, p_grade_band text, p_subject text)
returns text language sql immutable set search_path = '' as $$
  select case
    when coalesce(p_grade_band, '') in ('Pre-K', 'K-2') or coalesce(p_subject, '') = 'Picture books'
      or lower(coalesce(p_quantity, '') || ' ' || coalesce(p_name, '')) ~ '(board book|picture book)' then 'children'
    when lower(coalesce(p_quantity, '')) ~ '(hardcover|hardback|library binding|hard cover)' then 'hardcover'
    when lower(coalesce(p_quantity, '')) ~ '(paperback|mass market|trade paper|softcover|soft cover)' then 'paperback'
    else 'other' end
$$;
grant execute on function gik.book_format(text, text, text, text) to authenticated;

alter table gik.programs enable row level security;
alter table gik.value_guide enable row level security;
do $$
declare t text;
begin
  foreach t in array array['programs','value_guide'] loop
    execute format('drop policy if exists tep_read on gik.%I', t);
    execute format('create policy tep_read on gik.%I for select to authenticated using (gik.is_tep())', t);
    execute format('drop policy if exists staff_all on gik.%I', t);
    execute format('create policy staff_all on gik.%I for all to authenticated using (gik.is_staff()) with check (gik.is_staff())', t);
  end loop;
end $$;
grant select, insert, update, delete on gik.programs, gik.value_guide to authenticated;

-- ---------------------------------------------------------------- the value of every scan line
-- New or like-new: the item's store value (verified or not; status says which). Used: books take the thrift guide
-- value; anything else keeps its item value until staff set one. Damaged: no value.
create or replace view gik.v_receipt_values with (security_invoker = on) as
select r.receipt_id, r.donation_id, d.program_code, r.gtin, r.qty, r.condition, r.scanned_at, r.location,
       im.name, im.category, s.is_book, s.grade_band, s.subject,
       case when s.is_book then gik.book_format(p.quantity, im.name, s.grade_band, s.subject) end as book_format,
       case when r.condition = 'damaged' then 0
            when r.condition = 'used' and s.is_book then vg.unit_value
            else im.fmv_unit end                                                      as unit_value,
       case when r.condition = 'damaged' then 'Damaged: no value'
            when r.condition = 'used' and s.is_book then vg.label || ' (' || vg.status || ')'
            when im.fmv_unit is null then 'No value yet'
            else 'Store value (' || im.fmv_status || ')' end                          as value_basis,
       case when r.condition = 'used' and s.is_book then vg.status = 'approved'
            else im.fmv_status = 'verified' end                                       as value_final
from gik.receipts r
join gik.donations d on d.donation_id = r.donation_id
join gik.item_master im on im.gtin = r.gtin
left join gik.v_stock s on s.gtin = r.gtin
left join gik.products p on p.gtin = r.gtin
left join gik.value_guide vg on vg.book_format = gik.book_format(p.quantity, im.name, s.grade_band, s.subject)
                            and vg.condition = 'used';

-- ---------------------------------------------------------------- per-program stock and summary
create or replace view gik.v_program_stock with (security_invoker = on) as
with rin as (
  select d.program_code, r.gtin, sum(r.qty) as units
  from gik.receipts r join gik.donations d on d.donation_id = r.donation_id group by 1, 2),
rout as (
  select ds.program_code, l.gtin, sum(l.qty) as units
  from gik.distribution_lines l join gik.distributions ds on ds.distribution_id = l.distribution_id group by 1, 2)
select coalesce(rin.program_code, rout.program_code) as program_code, coalesce(rin.gtin, rout.gtin) as gtin,
       s.name, s.category, s.is_book, s.grade_band, s.subject, s.fmv_unit, s.fmv_status,
       coalesce(rin.units, 0) as received, coalesce(rout.units, 0) as distributed,
       coalesce(rin.units, 0) - coalesce(rout.units, 0) as on_hand
from rin full join rout on rout.program_code = rin.program_code and rout.gtin = rin.gtin
left join gik.v_stock s on s.gtin = coalesce(rin.gtin, rout.gtin);

create or replace view gik.v_program_summary with (security_invoker = on) as
select p.program_code, p.name, p.sort_order,
       (select count(*) from gik.donations d where d.program_code = p.program_code)                     as donations,
       coalesce((select sum(received) from gik.v_program_stock s where s.program_code = p.program_code), 0)    as units_received,
       coalesce((select sum(distributed) from gik.v_program_stock s where s.program_code = p.program_code), 0) as units_distributed,
       coalesce((select sum(greatest(on_hand, 0)) from gik.v_program_stock s where s.program_code = p.program_code), 0) as units_on_hand,
       coalesce((select round(sum(v.qty * v.unit_value), 2) from gik.v_receipt_values v where v.program_code = p.program_code), 0) as value_received_est,
       (select count(*) from gik.distributions ds where ds.program_code = p.program_code)               as visits,
       (select count(distinct ds.partner_id) from gik.distributions ds where ds.program_code = p.program_code) as partners_served,
       coalesce((select sum(ds.students_served) from gik.distributions ds where ds.program_code = p.program_code), 0) as students_served,
       coalesce((select sum(ds.classrooms_served) from gik.distributions ds where ds.program_code = p.program_code), 0) as classrooms_served
from gik.programs p where p.active;

create or replace view gik.v_program_stock_by_bin with (security_invoker = on) as
select program_code, coalesce(grade_band, 'Not a book') as grade_band, coalesce(subject, coalesce(category, 'Uncategorized')) as subject,
       count(*) as titles, sum(received) as received, sum(distributed) as distributed, sum(on_hand) as on_hand
from gik.v_program_stock group by 1, 2, 3;

grant select on gik.v_receipt_values, gik.v_program_stock, gik.v_program_summary, gik.v_program_stock_by_bin to authenticated;
