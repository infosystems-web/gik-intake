-- Packing lists and pickup totals value books that have no store price at the used-book value (gik.value_guide),
-- the same way intake does, instead of $0. Run in the Supabase SQL editor after programs_values.sql. Safe to re-run.

-- One value per item: the store value when there is one; for a book without one, the used-book guide value.
create or replace view gik.v_item_value with (security_invoker = on) as
select s.gtin, s.fmv_unit, s.fmv_status,
       case when s.fmv_unit is not null then s.fmv_unit
            when s.is_book then vg.unit_value end                                     as unit_value,
       case when s.fmv_unit is not null then 'Store value (' || s.fmv_status || ')'
            when s.is_book and vg.unit_value is not null then vg.label || ' (' || vg.status || ')'
            else 'No value yet' end                                                   as value_basis,
       case when s.fmv_unit is not null then s.fmv_status = 'verified'
            when s.is_book then coalesce(vg.status = 'approved', false)
            else false end                                                            as value_final
from gik.v_stock s
left join gik.products p on p.gtin = s.gtin
left join gik.value_guide vg on s.is_book and s.fmv_unit is null and vg.condition = 'used'
                            and vg.book_format = gik.book_format(p.quantity, s.name, s.grade_band, s.subject);

-- Same columns as before (fmv_unit stays the store value); value_est now uses unit_value; two columns added at the end.
create or replace view gik.v_distribution_items with (security_invoker = on) as
select l.distribution_id, l.gtin, s.name, s.brand, s.grade_band, s.subject, sum(l.qty) as qty,
       s.fmv_unit, round(sum(l.qty) * coalesce(v.unit_value, 0), 2) as value_est, s.fmv_status,
       v.unit_value, v.value_basis
from gik.distribution_lines l
join gik.v_stock s on s.gtin = l.gtin
left join gik.v_item_value v on v.gtin = l.gtin
group by l.distribution_id, l.gtin, s.name, s.brand, s.grade_band, s.subject, s.fmv_unit, s.fmv_status, v.unit_value, v.value_basis;

create or replace view gik.v_distribution_summary with (security_invoker = on) as
select d.distribution_id, d.distributed_on, p.partner_id, p.name as partner, d.students_served, d.classrooms_served,
       count(l.line_id) as lines, coalesce(sum(l.qty), 0) as units, count(distinct l.gtin) as titles,
       round(coalesce(sum(l.qty * v.unit_value), 0), 2) as value_est,
       round(coalesce(sum(l.qty * v.unit_value) filter (where v.value_final), 0), 2) as value_verified,
       coalesce(sum(l.qty) filter (where l.line_id is not null and v.unit_value is null), 0) as units_without_value,
       d.notes, d.created_by
from gik.distributions d
join gik.partners p on p.partner_id = d.partner_id
left join gik.distribution_lines l on l.distribution_id = d.distribution_id
left join gik.v_item_value v on v.gtin = l.gtin
group by d.distribution_id, p.partner_id, p.name;

grant select on gik.v_item_value, gik.v_distribution_items, gik.v_distribution_summary to authenticated;
