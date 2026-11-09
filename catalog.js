// GIK Catalog tab: directors' view, NetSuite item export, scanning prep. Loaded after the main script in index.html,
// uses its globals (sb, $, esc, toast, fail, download). Data comes from the gik.v_catalog* views (catalog_views.sql).
// ---------- catalog (directors' view, NetSuite prep, scanning) ----------
const num = (n) => (+n || 0).toLocaleString();
const money = (n) => "$" + (+n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0) + "%";
const CAT_PAGE = 100; let catPage = 0, catTimer = null;
window.onCatalogView = (v) => {
  clearInterval(catTimer); catTimer = null;
  if (v === "catalog") { loadCatalog(); catTimer = setInterval(() => { if (!document.hidden) loadCatalog(true); }, 60000); }
};
const CAT_COLS = "gtin,upc,name,brand,category,fmv_unit,fmv_basis,fmv_status,fmv_source_url,fmv_merchant,fmv_captured_at,price_source,origin,code_type,units_scanned,last_scanned,netsuite_ready,ns_item_id,debit_account_id,debit_account_name,credit_account_id,credit_account_name,class_id,class_name,created_at";
function catFilters(q) {
  const s = $("#catSearch").value.trim().replace(/[,()*%]/g, " ").trim();
  if (s) q = q.or(`name.ilike.*${s}*,brand.ilike.*${s}*,gtin.ilike.*${s}*,upc.ilike.*${s}*`);
  const c = $("#catCat").value, o = $("#catOrigin").value, p = $("#catSource").value, st = $("#catStatus").value, t = $("#catType").value;
  if (c) q = c === "Not categorized yet" ? q.is("category", null) : q.eq("category", c);
  if (o) q = q.eq("origin", o);
  if (p) q = q.eq("price_source", p);
  if (t) q = q.eq("code_type", t);
  if (st === "ready") q = q.eq("netsuite_ready", true);
  else if (st === "priced") q = q.not("fmv_unit", "is", null);
  else if (st === "noprice") q = q.is("fmv_unit", null);
  else if (st) q = q.eq("fmv_status", st);
  return q;
}
function fillSelect(sel, values, allLabel) {
  const cur = sel.value; sel.innerHTML = `<option value="">${allLabel}</option>` + values.map((v) => `<option>${esc(v)}</option>`).join(""); sel.value = values.includes(cur) ? cur : "";
}
async function loadCatalog(quiet) {
  const [sum, cats, srcs] = await Promise.all([
    sb.from("v_catalog_summary").select("*").single(),
    sb.from("v_catalog_by_category").select("*").order("items", { ascending: false }),
    sb.from("v_catalog_by_source").select("*").order("items", { ascending: false }),
  ]);
  for (const r of [sum, cats, srcs]) if (r.error) return quiet ? console.warn(r.error) : fail(r.error);
  const s = sum.data;
  $("#catStats").innerHTML = [
    ["Items in catalog", num(s.items), `${num(s.products)} products &middot; ${num(s.books)} books &middot; ${num(s.no_barcode)} no-barcode`],
    ["Valued with proof", pct(s.with_proof, s.items), `${num(s.with_proof)} items have a store page`],
    ["Units scanned", num(s.units_scanned), `${num(s.scanned_items)} different items`],
    ["Est. value scanned", money(s.scanned_value_est), `${money(s.scanned_value_verified)} verified by staff`],
    ["Ready for NetSuite", num(s.netsuite_ready), `${num(s.verified)} verified &middot; ${num(s.items - s.verified)} to review`],
    ["Added last 24 hours", num(s.added_24h), `${num(s.added_7d)} in the last 7 days`],
  ].map(([k, v, sub]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`).join("");
  $("#catUpdated").textContent = `Updated ${new Date().toLocaleTimeString()} · last lookup ${s.last_lookup ? new Date(s.last_lookup).toLocaleString() : "—"}`;
  const maxI = Math.max(1, ...cats.data.map((c) => c.items));
  $("#catByCat tbody").innerHTML = cats.data.map((c) => `<tr class="clk" data-cat="${esc(c.category)}"><td>${esc(c.category)}</td><td><div class="bar"><span style="width:${((100 * c.items) / maxI).toFixed(1)}%"></span><span class="p" style="width:${((100 * c.priced) / maxI).toFixed(1)}%"></span></div></td><td>${num(c.items)}</td><td>${pct(c.priced, c.items)}</td><td>${num(c.units_scanned)}</td><td>${c.avg_price != null ? "$" + (+c.avg_price).toFixed(2) : ""}</td><td class="small muted">${esc(c.credit_account || "")}</td></tr>`).join("");
  $("#catByCat").querySelectorAll("tr[data-cat]").forEach((tr) => (tr.onclick = () => { $("#catCat").value = tr.dataset.cat; catPage = 0; loadCatalogTable(); $("#catTable").scrollIntoView({ behavior: "smooth" }); }));
  $("#catBySrc").innerHTML = srcs.data.map((x) => `<span class="pill">${esc(x.price_source)}: ${num(x.items)}</span>`).join(" ");
  fillSelect($("#catCat"), cats.data.map((c) => c.category), "All categories");
  fillSelect($("#catSource"), srcs.data.map((x) => x.price_source), "Any value source");
  await loadCatalogTable(quiet);
}
async function loadCatalogTable(quiet) {
  const [col, dir] = $("#catSort").value.split(".");
  let q = sb.from("v_catalog").select("gtin,upc,name,brand,category,fmv_unit,fmv_status,fmv_source_url,fmv_merchant,price_source,origin,code_type,units_scanned,netsuite_ready", { count: "exact" });
  q = catFilters(q).order(col, { ascending: dir === "asc", nullsFirst: false }).order("gtin").range(catPage * CAT_PAGE, catPage * CAT_PAGE + CAT_PAGE - 1);
  const { data, error, count } = await q;
  if (error) return quiet ? console.warn(error) : fail(error);
  const total = count || 0;
  $("#catCount").textContent = `${num(total)} items`;
  $("#catPageInfo").textContent = total ? `${num(catPage * CAT_PAGE + 1)}–${num(Math.min(total, (catPage + 1) * CAT_PAGE))} of ${num(total)}` : "No items match";
  $("#catPrev").disabled = catPage === 0; $("#catNext").disabled = (catPage + 1) * CAT_PAGE >= total;
  $("#catTable tbody").innerHTML = data.map((r) => {
    const status = r.netsuite_ready ? '<span class="pill ok">Ready for NetSuite</span>' : r.fmv_status === "verified" ? '<span class="pill ok">Verified</span>' : r.fmv_status === "not_found" ? '<span class="pill bad">Not found</span>' : r.fmv_unit != null ? '<span class="pill warn">Needs review</span>' : '<span class="pill">No value yet</span>';
    return `<tr><td class="small">${esc(r.upc)}<div class="muted">${esc(r.code_type)}</div></td><td>${esc(r.name || "(name not found yet)")}<div class="small muted">${esc(r.brand || "")}${r.brand ? " &middot; " : ""}${esc(r.origin)}</div></td><td class="small">${esc(r.category || "—")}</td><td>${r.fmv_unit != null ? "$" + (+r.fmv_unit).toFixed(2) : ""}<div class="small muted">${esc(r.price_source)}</div></td><td class="small">${r.fmv_source_url ? `<a href="${esc(r.fmv_source_url)}" target="_blank" rel="noopener">${esc((r.fmv_merchant || "store page").slice(0, 28))}</a>` : ""}</td><td>${r.units_scanned ? num(r.units_scanned) : ""}</td><td>${status}</td></tr>`;
  }).join("");
}
let catDeb = null;
$("#catSearch").oninput = () => { clearTimeout(catDeb); catDeb = setTimeout(() => { catPage = 0; loadCatalogTable(); }, 300); };
["#catCat", "#catType", "#catOrigin", "#catStatus", "#catSource", "#catSort"].forEach((id) => ($(id).onchange = () => { catPage = 0; loadCatalogTable(); }));
$("#catPrev").onclick = () => { if (catPage > 0) { catPage--; loadCatalogTable(); } };
$("#catNext").onclick = () => { catPage++; loadCatalogTable(); };
$("#btnCatLink").onclick = async () => { const u = location.origin + location.pathname + "#catalog"; try { await navigator.clipboard.writeText(u); toast("Link copied. Directors sign in with their TEP Google account.", "ok"); } catch (e) { prompt("Copy this link:", u); } };
async function fetchCatalog(extra) {
  let out = [];
  for (let from = 0; ; from += 1000) {
    let q = catFilters(sb.from("v_catalog").select(CAT_COLS)); if (extra) q = extra(q);
    const { data, error } = await q.order("gtin").range(from, from + 999);
    if (error) throw error;
    out = out.concat(data);
    if (data.length < 1000 || out.length >= 200000) return out;
  }
}
const today = () => new Date().toISOString().slice(0, 10);
$("#btnCatCsv").onclick = async () => {
  toast("Preparing the list…");
  try {
    const data = await fetchCatalog();
    const rows = [["barcode_gtin", "upc_or_isbn", "type", "item", "brand", "category", "unit_value", "value_basis", "value_source", "proof_merchant", "proof_url", "value_date", "status", "ready_for_netsuite", "units_scanned", "last_scanned", "origin", "added"]];
    for (const r of data) rows.push([r.gtin, r.upc, r.code_type, r.name, r.brand, r.category, r.fmv_unit, r.fmv_basis, r.price_source, r.fmv_merchant, r.fmv_source_url, (r.fmv_captured_at || "").slice(0, 10), r.fmv_status, r.netsuite_ready ? "yes" : "no", r.units_scanned, (r.last_scanned || "").slice(0, 10), r.origin, (r.created_at || "").slice(0, 10)]);
    download(`gik_catalog_${today()}.csv`, rows); toast(`${num(data.length)} items downloaded`, "ok");
  } catch (e) { fail(e); }
};
$("#btnNsCsv").onclick = async () => {
  const draft = $("#nsDraft").checked;
  toast("Preparing the NetSuite file…");
  try {
    const data = await fetchCatalog((q) => (draft ? q.not("fmv_unit", "is", null).not("category", "is", null) : q.eq("netsuite_ready", true)));
    if (!data.length) return toast(draft ? "No valued, categorized items match these filters" : "No items are verified yet. Tick “include items not yet verified” for a draft file.", "bad");
    const rows = [["External ID", "Item Name/Number", "Display Name", "UPC Code", "Description", "Manufacturer", "TEP Category", "Unit Value (FMV)", "Value Basis", "Debit Account", "Credit Account", "Class", "Value Source", "Proof URL", "Value Date", "NetSuite Internal ID", "Import Status"]];
    for (const r of data) rows.push([r.gtin, r.upc, (r.name || "").slice(0, 60), r.code_type === "No barcode" ? "" : r.upc, r.name, r.brand, r.category, r.fmv_unit, r.fmv_basis, [r.debit_account_id, r.debit_account_name].filter(Boolean).join(" "), [r.credit_account_id, r.credit_account_name].filter(Boolean).join(" "), [r.class_id, r.class_name].filter(Boolean).join(" "), r.price_source, r.fmv_source_url, (r.fmv_captured_at || "").slice(0, 10), r.ns_item_id, r.netsuite_ready ? "READY" : "DRAFT - not verified"]);
    download(`gik_netsuite_items_${draft ? "DRAFT_" : ""}${today()}.csv`, rows); toast(`${num(data.length)} items in the NetSuite file`, "ok");
  } catch (e) { fail(e); }
};
