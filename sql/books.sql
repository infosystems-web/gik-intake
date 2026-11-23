-- Book dataset: every book in the reference catalog (gik.products, ISBN barcodes) with its fields split out and its
-- sorting bin (grade band + subject) worked out ahead of time, so books can be browsed, counted and sorted before any
-- are scanned, and a scan shows its bin at once. Run in the Supabase SQL editor after programs_values.sql. Re-run safe.
-- Fill it with:  select gik.refresh_book_catalog(50000);   (repeat until it returns 0; the weekly job does this too)
-- After changing the sorting rules, re-sort every book:  update gik.book_catalog set source_updated_at = null;
-- then run the refresh again (Actions > GIK reference catalog > sources: books).

-- ---------------------------------------------------------------- sorting rules for Open Library data
-- Open Library gives subject headings ("Juvenile fiction", "Alphabet books", "Board books", "Young adult fiction"),
-- a binding ("Board book", "Paperback") and sometimes a page count. Title words that name a grade win first.
create or replace function gik.ol_grade_band(p_title text, p_subjects text, p_pages int, p_binding text)
returns text language sql immutable set search_path = '' as $$
  select case
    -- 1. the title names a grade or level
    when t ~ '(pre-?k\M|preschool|toddler|baby''?s? first|ages 0|ages 1-3|ages 2-4)' then 'Pre-K'
    when t ~ '(kindergarten|grade ?k\M|\mgrades? ?(1|2)\M|first grade|second grade|1st grade|2nd grade|sight words|decodable|level 1\M|level 2\M|pre-level)' then 'K-2'
    when t ~ '(\mgrades? ?(3|4|5)\M|third grade|fourth grade|fifth grade|3rd grade|4th grade|5th grade|level 3\M|level 4\M)' then '3-5'
    when t ~ '(\mgrades? ?(6|7|8)\M|sixth grade|seventh grade|eighth grade|middle school)' then '6-8'
    when t ~ '(\mgrades? ?(9|10|11|12)\M|high school|\msat\M|\mact\M|\map (biology|chemistry|english|history|calculus|psychology|u\.s\.))' then '9-12'
    -- 2. binding
    when b ~ 'board' then 'Pre-K'
    -- 3. subjects
    when s ~ '(board books|toddler|baby books|nursery rhymes|lullab)' then 'Pre-K'
    when s ~ '(young adult|\mteen|high school|ya fiction)' then '9-12'
    when s ~ '(middle school|middle grade)' then '6-8'
    when s ~ '(education|teaching|teachers|classroom management|curricul|study and teaching|lesson plan)' and s !~ '(juvenile|children)' then 'Teacher/Adult'
    when s ~ '(alphabet books|concept books|counting|picture books|stories in rhyme|bedtime|easy readers|beginning reader|readers \(primary\)|early reader|picture puzzle|toy and movable)' then 'K-2'
    when s ~ '(chapter books|readers \(elementary\)|intermediate)' then '3-5'
    when s ~ '(phonics|reading \(elementary\)|reading \(primary\)|sight words)' then 'K-2'
    when s ~ '(juvenile|children|spanish language materials|fairy tales|folklore|friendship|coloring books|workbooks|graphic novels)' then case
           when p_pages is null then 'K-5 (check pages)'
           when p_pages <= 48 then 'K-2'
           when p_pages <= 250 then '3-5'
           else '6-8' end
    else 'Not sorted yet' end
  from (select lower(coalesce(p_title, '')) as t, lower(coalesce(p_subjects, '')) as s, lower(coalesce(p_binding, '')) as b) x
$$;

create or replace function gik.ol_subject_bin(p_title text, p_subjects text, p_grade text)
returns text language sql immutable set search_path = '' as $$
  select case
    when s ~ '(spanish language|bilingual|spanish)' or t ~ '(spanish edition|en español|bilingual)' then 'Spanish & bilingual'
    when s ~ '(activity books|coloring|workbooks|puzzles|sticker|games|amusements|handicraft)' or t ~ '(workbook|activity book|coloring|sticker|flash ?cards|brain quest|summer bridge|test prep)' then 'Workbooks & activities'
    when s ~ '(study and teaching|education|teaching|teachers|classroom management|curricul|lesson plan)' and s !~ '(juvenile|children)' then 'Teacher resources'
    when s ~ '(graphic novels|comic books|comics)' or t ~ 'graphic novel' then 'Graphic novels & comics'
    when s ~ '(biograph)' or t ~ '^who (was|is)' then 'Biography'
    when s ~ '(mathematic|arithmetic|counting|addition|subtraction|multiplication|fractions|geometry|shapes|measurement)' then 'Math'
    when s ~ '(science|animals|nature|dinosaurs|space|astronomy|planets|weather|plants|insects|ocean|marine|ecology|environment|human body|technology|engineering|machines|vehicles|trucks)' and s !~ 'fiction' then 'Science & nature'
    when s ~ '(history|social studies|holidays|geography|countries|civil rights|presidents|government|communities)' and s !~ 'fiction' then 'History & social studies'
    when s ~ '(emotions|feelings|friendship|bullying|social skills|self-esteem|manners|health|safety|kindness)' and s !~ '(fiction|stories)' then 'Health & social-emotional'
    when s ~ '(dictionar|atlas|encyclopedia|reference)' then 'Reference'
    when s ~ '(\mart\M|music|drawing|crafts|dance|painting)' and s !~ 'fiction' then 'Arts & music'
    when s ~ '(easy readers|beginning reader|readers \(|early reader|chapter books)' then 'Early readers & chapter books'
    when s ~ '(phonics|reading|language arts|vocabulary|grammar|spelling|handwriting|writing)' then 'Reading & language arts'
    when s ~ '(poetry|nursery rhymes|stories in rhyme)' then 'Poetry & rhymes'
    when s ~ '(picture books|board books|alphabet books|concept books|bedtime|toy and movable)' or p_grade in ('Pre-K', 'K-2') and s ~ '(fiction|stories|juvenile)' then 'Picture books'
    when s ~ '(fiction|stories|fantasy|mystery|adventure|novel|fairy tales|folklore|legends)' then 'Fiction'
    when s ~ '(juvenile literature|juvenile nonfiction|nonfiction)' then 'Other nonfiction'
    when p_grade = 'Teacher/Adult' then 'Teacher resources'
    else 'Not sorted yet' end
  from (select lower(coalesce(p_title, '')) as t, lower(coalesce(p_subjects, '')) as s) x
$$;
grant execute on function gik.ol_grade_band(text, text, int, text), gik.ol_subject_bin(text, text, text) to authenticated;

-- ---------------------------------------------------------------- the dataset
create table if not exists gik.book_catalog (
  gtin         text primary key,              -- 0 + ISBN-13, same key as products / item_master
  isbn13       text not null,
  title        text,
  author       text,
  publisher    text,
  pages        int,
  binding      text,                          -- Board book, Paperback, Hardcover ... (sets the used-book value)
  subjects     text,                          -- Open Library subject headings
  grade_band   text,
  subject      text,
  cover_url    text,
  source       text,                          -- openlibrary-dump | openlibrary | ...
  source_url   text,
  source_updated_at timestamptz,              -- products.updated_at when this row was worked out
  computed_at  timestamptz not null default now()
);
create index if not exists idx_book_catalog_bin on gik.book_catalog (grade_band, subject);
create index if not exists idx_book_catalog_title on gik.book_catalog (lower(title) text_pattern_ops);

alter table gik.book_catalog enable row level security;
drop policy if exists tep_read on gik.book_catalog;
create policy tep_read on gik.book_catalog for select to authenticated using (gik.is_tep());
drop policy if exists staff_all on gik.book_catalog;
create policy staff_all on gik.book_catalog for all to authenticated using (gik.is_staff()) with check (gik.is_staff());
grant select on gik.book_catalog to authenticated;

-- Counts per bin, kept as a small table so the page loads at once instead of counting millions of rows.
create table if not exists gik.book_bin_counts (
  grade_band text not null,
  subject    text not null,
  titles     bigint not null,
  updated_at timestamptz not null default now(),
  primary key (grade_band, subject)
);
alter table gik.book_bin_counts enable row level security;
drop policy if exists tep_read on gik.book_bin_counts;
create policy tep_read on gik.book_bin_counts for select to authenticated using (gik.is_tep());
grant select on gik.book_bin_counts to authenticated;

-- Work out (or re-work) up to p_limit books that are new or changed in gik.products. Returns how many it did;
-- call again until it returns 0. When nothing is left it refreshes the bin counts.
create or replace function gik.refresh_book_catalog(p_limit int default 50000)
returns int language plpgsql security definer set search_path = '' as $$
declare n int;
begin
  insert into gik.book_catalog as b (gtin, isbn13, title, author, publisher, pages, binding, subjects, grade_band, subject,
                                     cover_url, source, source_url, source_updated_at, computed_at)
  select x.gtin, substr(x.gtin, 2), x.title, x.author, x.publisher, x.pages, x.binding, x.subjects,
         x.grade, gik.ol_subject_bin(x.title, x.subjects, x.grade), x.image_url, x.source, x.source_url, x.updated_at, now()
  from (
    select p.gtin, p.image_url, p.source, p.source_url, p.updated_at, p.brand as publisher,
           nullif(regexp_replace(p.name, ' - by .*$', ''), '') as title,
           substring(p.name from ' - by (.*)$') as author,
           nullif(substring(p.quantity from '(\d+) pages'), '')::int as pages,
           nullif(btrim(regexp_replace(coalesce(p.quantity, ''), '^\d+ pages,?\s*', '')), '') as binding,
           nullif(regexp_replace(coalesce(p.raw_categories, ''), '^Books > ', ''), '') as subjects,
           gik.ol_grade_band(regexp_replace(p.name, ' - by .*$', ''), p.raw_categories,
                             nullif(substring(p.quantity from '(\d+) pages'), '')::int,
                             regexp_replace(coalesce(p.quantity, ''), '^\d+ pages,?\s*', '')) as grade
    from gik.products p
    where substr(p.gtin, 2, 3) in ('978', '979')
      and not exists (select 1 from gik.book_catalog c where c.gtin = p.gtin and c.source_updated_at >= p.updated_at)
    limit p_limit) x
  on conflict (gtin) do update set
    title = excluded.title, author = excluded.author, publisher = excluded.publisher, pages = excluded.pages,
    binding = excluded.binding, subjects = excluded.subjects, grade_band = excluded.grade_band, subject = excluded.subject,
    cover_url = excluded.cover_url, source = excluded.source, source_url = excluded.source_url,
    source_updated_at = excluded.source_updated_at, computed_at = now();
  get diagnostics n = row_count;
  if n = 0 then
    delete from gik.book_bin_counts;
    insert into gik.book_bin_counts (grade_band, subject, titles)
    select coalesce(grade_band, 'Not sorted yet'), coalesce(subject, 'Not sorted yet'), count(*) from gik.book_catalog group by 1, 2;
  end if;
  return n;
end $$;
revoke all on function gik.refresh_book_catalog(int) from public;
revoke all on function gik.refresh_book_catalog(int) from authenticated;
