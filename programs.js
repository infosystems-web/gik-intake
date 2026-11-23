// Programs, condition at scan, sorting bins, Distribute (scan-out) and Programs tabs.
// Loaded by catalog.js after index.html's own script, so it can use sb, $, esc, toast, fail, user, isStaff, state,
// queue, sessionRows, show, recordScan and loadRefs. Needs sql/programs_values.sql applied in Supabase.
(() => {
  const money = (n) => "$" + (+n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const num = (n) => (+n || 0).toLocaleString();
  const isIsbn = (g) => /^0?97[89]/.test(g) && g.length === 14;
  const DEVICE = localStorage.getItem("gik.device") || (() => { const d = "dev-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("gik.device", d); return d; })();
  let programs = [], partners = [], visit = null, visitLines = [];
  const progOptions = (sel) => programs.map((p) => `<option value="${esc(p.program_code)}"${p.program_code === sel ? " selected" : ""}>${esc(p.name)}</option>`).join("");

  // ------------------------------------------------------------ build the new screens
  const nav = $("#nav");
  for (const [v, label] of [["distribute", "Distribute"], ["programs", "Programs"]]) {
    const b = document.createElement("button"); b.dataset.v = v; b.textContent = label; b.onclick = () => show(v); nav.appendChild(b);
  }
  const main = document.querySelector("main");
  main.insertAdjacentHTML("beforeend", `
<section id="v-distribute" class="hidden">
  <div class="card" id="distStart"><h2>Start a pickup</h2>
    <p class="small muted">A partner nonprofit picks up items: pick the program and the partner, then scan every item going out. Each pickup gets an ID (OUT-YYYY-NNNN), a packing list and its own totals.</p>
    <div class="row">
      <div><label>Program</label><select id="dProg"></select></div>
      <div><label>Partner nonprofit</label><select id="dPartner"></select></div>
      <div style="flex:0 0 150px"><label>Date</label><input id="dOn" type="date"></div>
    </div>
    <div class="row">
      <div><label>…or new partner name</label><input id="dNewPartner" placeholder="Westside Elementary PTO"></div>
      <div><label>City</label><input id="dNewCity" placeholder="Pittsburgh"></div>
      <div><label>Contact</label><input id="dNewContact"></div>
    </div>
    <div class="row">
      <div style="flex:0 0 160px"><label>Students served</label><input id="dStudents" type="number" min="0"></div>
      <div style="flex:0 0 160px"><label>Classrooms served</label><input id="dClassrooms" type="number" min="0"></div>
      <div><label>Notes</label><input id="dVisitNotes"></div>
    </div>
    <p><button class="btn" id="btnStartVisit">Start pickup</button></p>
  </div>
  <div class="card hidden" id="distActive"><h2>Scanning out <span id="dVisitTitle"></span></h2>
    <div class="row"><div><label>Scan each item going out (q24 = case of 24, - = undo last)</label><input id="dScan" class="scanbox" autocomplete="off" placeholder="scan here…"></div>
      <div style="flex:0 0 110px"><label>Qty per scan</label><input id="dQty" type="number" min="1" value="1"></div></div>
    <div id="dLast" class="lastscan hidden"></div>
    <div class="row" style="align-items:center"><h2 style="flex:1;margin-top:10px">This pickup</h2><div class="big" id="dUnits">0</div><div class="muted small" style="flex:0">units</div></div>
    <div style="overflow:auto"><table id="dLines"><thead><tr><th>Item</th><th>Bin</th><th>Qty</th><th>Left in program</th></tr></thead><tbody></tbody></table></div>
    <p><button class="btn" id="btnFinishVisit">Finish and show packing list</button> <button class="btn sec" id="btnLeaveVisit">Leave open for later</button></p>
  </div>
  <div class="card hidden" id="distPacking"><h2>Packing list <span id="pkTitle"></span></h2>
    <div id="pkHead" class="small"></div>
    <div style="overflow:auto"><table id="pkTable"><thead><tr><th>Item</th><th>Grade</th><th>Subject</th><th>Qty</th><th>Value each</th><th>Value</th></tr></thead><tbody></tbody></table></div>
    <p><button class="btn" id="btnPrintPk">Print</button> <button class="btn sec" id="btnPkCsv">Download CSV</button> <button class="btn sec" id="btnPkClose">Done</button></p>
  </div>
  <div class="card"><h2>Recent pickups</h2>
    <div style="overflow:auto"><table id="dRecent"><thead><tr><th>ID</th><th>Date</th><th>Program</th><th>Partner</th><th>Units</th><th>Titles</th><th>Value (est.)</th><th>Students</th><th></th></tr></thead><tbody></tbody></table></div>
  </div>
</section>
<section id="v-programs" class="hidden">
  <div class="card"><h2>Programs</h2>
    <p class="small muted">One shared catalog, branched into programs. Each program has its own donations, stock, pickups and totals. A scan belongs to the program of its donation; a pickup to the program chosen when it starts.</p>
    <div style="overflow:auto"><table id="pgTable"><thead><tr><th>Program</th><th>Donations</th><th>Units in</th><th>Units out</th><th>On hand</th><th>Value in (est.)</th><th>Pickups</th><th>Partners</th><th>Students</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div class="card"><div class="row" style="align-items:center"><h2 style="flex:1;margin:0">Stock by sorting bin</h2><div style="flex:0 0 260px"><select id="pgPick"></select></div></div>
    <div style="overflow:auto;margin-top:8px"><table id="pgBins"><thead><tr><th>Grade band</th><th>Subject / category</th><th>Titles</th><th>Received</th><th>Out</th><th>On hand</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div class="card"><h2>Used-book values <span class="small muted">(thrift value, IRS Pub. 561)</span></h2>
    <p class="small muted">Books scanned as <b>Used</b> take these values; new books take their store or publisher price. Amounts are a TEP estimate from published thrift guides until finance approves them. <span id="vgStaffNote"></span></p>
    <div style="overflow:auto"><table id="vgTable"><thead><tr><th>Book type</th><th>Value each</th><th>Status</th><th>Sources</th><th></th></tr></thead><tbody></tbody></table></div>
  </div>
</section>`);

  // The lookup job runs every hour since 11/9 (the Catalog text still said every 3 hours).
  document.querySelectorAll("#v-catalog p").forEach((p) => { p.innerHTML = p.innerHTML.replace("every 3 hours", "every hour"); });

  // ------------------------------------------------------------ scan tab: condition + sorting bin
  const qtyBox = $("#qty").parentElement;
  qtyBox.insertAdjacentHTML("afterend", `<div style="flex:0 0 150px"><label>Condition</label><select id="cond"><option value="new">New</option><option value="like-new">Like new</option><option value="used">Used</option><option value="damaged">Damaged</option></select></div>`);
  $("#cond").value = localStorage.getItem("gik.cond") || "new";
  $("#cond").onchange = () => localStorage.setItem("gik.cond", $("#cond").value);

  async function binFor(gtin) {
    const [im, pr] = await Promise.all([
      sb.from("item_master").select("name, raw_category, page_count, grade_band_override, subject_override").eq("gtin", gtin).maybeSingle(),
      sb.from("products").select("name, quantity, raw_categories").eq("gtin", gtin).maybeSingle()]);
    const name = im.data?.name || pr.data?.name || "", cat = im.data?.raw_category || pr.data?.raw_categories || "";
    const pages = im.data?.page_count || +((pr.data?.quantity || "").match(/(\d+)\s*pages/) || [])[1] || null;
    const [g, s] = await Promise.all([
      im.data?.grade_band_override ? { data: im.data.grade_band_override } : sb.rpc("book_grade_band", { p_name: name, p_cat: cat, p_pages: pages }),
      im.data?.subject_override ? { data: im.data.subject_override } : sb.rpc("book_subject", { p_name: name, p_cat: cat, p_pages: pages })]);
    return { name, grade: g.data || "", subject: s.data || "" };
  }
  const baseRecord = recordScan;
  recordScan = function (code) {
    baseRecord(code);
    const cond = $("#cond").value;
    const q = queue[queue.length - 1]; if (q && q.gtin === code) { q.condition = cond; saveQueue(); }
    if (sessionRows[0] && sessionRows[0].gtin === code) sessionRows[0].condition = cond;
    if (cond !== "new") $("#last").insertAdjacentHTML("beforeend", `<span class="pill">${esc(cond)}</span>`);
    if (isIsbn(code)) binFor(code).then((b) => {
      if (!b.grade || state.lastReceipt?.gtin !== code) return;
      $("#last").insertAdjacentHTML("beforeend", `<div style="margin-left:auto;text-align:right"><div class="small muted">Sorting bin</div><b>${esc(b.grade)}</b> · ${esc(b.subject)}</div>`);
    }).catch(() => {});
  };

  // ------------------------------------------------------------ donations: program
  $("#dNotes").parentElement && $("#dNotes").insertAdjacentHTML("beforebegin", `<label>Program</label><select id="dProgram"></select>`);
  $("#btnDonation").onclick = async () => {
    let donorId = $("#donorSel").value; const newName = $("#donorNew").value.trim();
    if (!donorId && !newName) return toast("Pick a donor or type a new donor name", "bad");
    if (newName) { const d = await sb.from("donors").insert({ name: newName, type: $("#donorType").value }).select().single(); if (d.error) return fail(d.error); donorId = d.data.donor_id; }
    const yr = ($("#dDate").value || new Date().toISOString()).slice(0, 4);
    const { data: ex } = await sb.from("donations").select("donation_id").like("donation_id", `DON-${yr}-%`).order("donation_id", { ascending: false }).limit(1);
    const n = ex?.length ? +ex[0].donation_id.split("-")[2] + 1 : 1;
    const donation_id = `DON-${yr}-${String(n).padStart(4, "0")}`;
    const ins = await sb.from("donations").insert({ donation_id, donor_id: donorId, received_date: $("#dDate").value, bol_number: $("#dBol").value || null,
      pallets: +$("#dPallets").value || null, notes: $("#dNotes").value || null, created_by: user.email,
      ...(programs.length ? { program_code: $("#dProgram").value || "TEP-GIK" } : {}) });   // before the SQL is applied: no program column yet
    if (ins.error) return fail(ins.error);
    toast(`Created ${donation_id}`, "ok"); $("#donorNew").value = ""; $("#dBol").value = ""; $("#dPallets").value = ""; $("#dNotes").value = "";
    await loadRefs(); $("#donation").value = donation_id; localStorage.setItem("gik.donation", donation_id); show("donations");
  };

  const baseLoadRefs = loadRefs;
  loadRefs = async function () {
    await baseLoadRefs();
    await loadProgramRefs();
  };
  async function loadProgramRefs() {
    const [p, pa, d] = await Promise.all([
      sb.from("programs").select("program_code, name").eq("active", true).order("sort_order"),
      sb.from("partners").select("partner_id, name, city").eq("active", true).order("name"),
      sb.from("donations").select("donation_id, program_code").order("received_date", { ascending: false }).limit(50)]);
    if (p.error) { console.warn("programs not set up yet (run sql/programs_values.sql)", p.error); return; }
    programs = p.data; partners = pa.data || [];
    const prog = Object.fromEntries((d.data || []).map((x) => [x.donation_id, x.program_code]));
    const name = Object.fromEntries(programs.map((x) => [x.program_code, x.name]));
    [...$("#donation").options].forEach((o) => { if (prog[o.value] && !o.textContent.startsWith("[")) o.textContent = `[${name[prog[o.value]] || prog[o.value]}] ` + o.textContent; });
    $("#dProgram").innerHTML = progOptions("TEP-GIK");
    $("#dProg").innerHTML = progOptions(localStorage.getItem("gik.distProg") || "TEP-BOOKS");
    $("#pgPick").innerHTML = progOptions(localStorage.getItem("gik.distProg") || "TEP-BOOKS");
    $("#dPartner").innerHTML = `<option value="">— pick —</option>` + partners.map((x) => `<option value="${x.partner_id}">${esc(x.name)}${x.city ? " (" + esc(x.city) + ")" : ""}</option>`).join("");
    $("#dOn").value = new Date().toISOString().slice(0, 10);
  }

  // ------------------------------------------------------------ distribute
  $("#dProg").onchange = () => localStorage.setItem("gik.distProg", $("#dProg").value);
  $("#btnStartVisit").onclick = async () => {
    let partnerId = $("#dPartner").value; const newName = $("#dNewPartner").value.trim();
    if (!partnerId && !newName) return toast("Pick a partner or type a new partner name", "bad");
    if (newName) {
      const p = await sb.from("partners").insert({ name: newName, city: $("#dNewCity").value || null, contact: $("#dNewContact").value || null, created_by: user.email }).select().single();
      if (p.error) return fail(p.error); partnerId = p.data.partner_id;
    }
    const yr = ($("#dOn").value || new Date().toISOString()).slice(0, 4);
    for (let attempt = 0; attempt < 3; attempt++) {
      const { data: ex } = await sb.from("distributions").select("distribution_id").like("distribution_id", `OUT-${yr}-%`).order("distribution_id", { ascending: false }).limit(1);
      const n = ex?.length ? +ex[0].distribution_id.split("-")[2] + 1 : 1;
      const id = `OUT-${yr}-${String(n).padStart(4, "0")}`;
      const ins = await sb.from("distributions").insert({ distribution_id: id, partner_id: +partnerId, program_code: $("#dProg").value, distributed_on: $("#dOn").value,
        students_served: $("#dStudents").value === "" ? null : +$("#dStudents").value, classrooms_served: $("#dClassrooms").value === "" ? null : +$("#dClassrooms").value,
        notes: $("#dVisitNotes").value || null, created_by: user.email }).select().single();
      if (!ins.error) { $("#dNewPartner").value = $("#dNewCity").value = $("#dNewContact").value = $("#dStudents").value = $("#dClassrooms").value = $("#dVisitNotes").value = "";
        await loadProgramRefs(); return openVisit(ins.data); }
      if (ins.error.code !== "23505") return fail(ins.error);   // someone took that ID at the same moment: try the next one
    }
  };
  async function openVisit(d) {
    visit = d; localStorage.setItem("gik.visit", d.distribution_id);
    const p = partners.find((x) => x.partner_id === d.partner_id);
    $("#dVisitTitle").textContent = `${d.distribution_id} · ${p ? p.name : ""} · ${programs.find((x) => x.program_code === d.program_code)?.name || d.program_code}`;
    $("#distStart").classList.add("hidden"); $("#distPacking").classList.add("hidden"); $("#distActive").classList.remove("hidden");
    const { data } = await sb.from("distribution_lines").select("line_id, gtin, qty, item_master(name)").eq("distribution_id", d.distribution_id).order("line_id", { ascending: false });
    visitLines = (data || []).map((l) => ({ ...l, name: l.item_master?.name }));
    renderVisit(); setTimeout(() => $("#dScan").focus(), 50);
  }
  function renderVisit() {
    $("#dUnits").textContent = num(visitLines.reduce((a, l) => a + l.qty, 0));
    $("#dLines tbody").innerHTML = visitLines.slice(0, 200).map((l) => `<tr><td>${esc(l.name || l.gtin)}<div class="small muted">${esc(l.gtin)}</div></td><td class="small">${esc(l.bin || "")}</td><td>${l.qty}</td><td>${l.left ?? ""}</td></tr>`).join("");
  }
  let dPendingQty = null;
  $("#dScan").addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return; e.preventDefault();
    const raw = $("#dScan").value.trim(); $("#dScan").value = ""; if (!raw || !visit) return;
    const m = raw.match(/^q(\d{1,4})$/i); if (m) { dPendingQty = +m[1]; return toast(`Next scan counts as ${dPendingQty}`); }
    if (raw === "-") return undoLine();
    const code = /^G-\d+$/i.test(raw) ? raw.toUpperCase() : normalizeGtin(raw);
    if (!code) return toast(`${raw} is not a valid barcode`, "bad");
    const qty = dPendingQty || Math.max(1, +$("#dQty").value || 1); dPendingQty = null;
    try {
      const im = await sb.from("item_master").upsert([{ gtin: code, fmv_status: "unverified" }], { onConflict: "gtin", ignoreDuplicates: true });
      if (im.error) throw im.error;
      const at = new Date().toISOString();
      const ins = await sb.from("distribution_lines").insert({ distribution_id: visit.distribution_id, gtin: code, qty, scanned_by: user.email, scanned_at: at, origin: `${DEVICE}|${at}|${code}` }).select("line_id").single();
      if (ins.error) throw ins.error;
      const line = { line_id: ins.data.line_id, gtin: code, qty, name: "" };
      visitLines.unshift(line); renderVisit();
      const [n, st] = await Promise.all([
        sb.from("item_master").select("name").eq("gtin", code).maybeSingle(),
        sb.from("v_program_stock").select("on_hand").eq("program_code", visit.program_code).eq("gtin", code).maybeSingle()]);
      line.name = n.data?.name || (await sb.from("products").select("name").eq("gtin", code).maybeSingle()).data?.name || "";
      line.left = st.data ? st.data.on_hand : 0;
      if (isIsbn(code)) { const b = await binFor(code); line.bin = b.grade ? `${b.grade} · ${b.subject}` : ""; }
      $("#dLast").classList.remove("hidden");
      $("#dLast").innerHTML = `<div class="big">${qty}</div><div><b>${esc(line.name || code)}</b><div class="small muted">${esc(line.bin || "")}${line.left < 0 ? ' · <span style="color:var(--bad)">more out than this program received — check the donation\'s program</span>' : ""}</div></div>`;
      if (navigator.vibrate) navigator.vibrate(30);
      renderVisit();
    } catch (err) { fail(err); }
  });
  async function undoLine() {
    const l = visitLines[0]; if (!l) return;
    const del = await sb.from("distribution_lines").delete().eq("line_id", l.line_id);
    if (del.error) return fail(del.error);
    visitLines.shift(); renderVisit(); toast("Removed the last item", "ok");
  }
  $("#btnLeaveVisit").onclick = () => { visit = null; localStorage.removeItem("gik.visit"); $("#distActive").classList.add("hidden"); $("#distStart").classList.remove("hidden"); loadRecentVisits(); };
  $("#btnFinishVisit").onclick = () => { const id = visit.distribution_id; visit = null; localStorage.removeItem("gik.visit"); showPacking(id); };

  let pkRows = [], pkId = "";
  // Value each: store value, or for a book without one the used-book value (sql/packing_values.sql adds unit_value).
  const unitOf = (r) => (r.unit_value !== undefined ? r.unit_value : r.fmv_unit);
  async function showPacking(id) {
    pkId = id;
    const [s, items] = await Promise.all([
      sb.from("v_distribution_summary").select("*").eq("distribution_id", id).maybeSingle(),
      sb.from("v_distribution_items").select("*").eq("distribution_id", id).order("grade_band").order("subject").order("name")]);
    if (s.error || items.error) return fail(s.error || items.error);
    pkRows = items.data;
    $("#distActive").classList.add("hidden"); $("#distStart").classList.add("hidden"); $("#distPacking").classList.remove("hidden");
    $("#pkTitle").textContent = id;
    const d = s.data || {};
    $("#pkHead").innerHTML = `<p><b>${esc(d.partner || "")}</b> · ${esc(d.distributed_on || "")} · ${num(d.units)} items, ${num(d.titles)} titles · estimated value ${money(d.value_est)}${d.students_served ? ` · ${num(d.students_served)} students` : ""}${d.classrooms_served ? ` · ${num(d.classrooms_served)} classrooms` : ""}</p>`;
    $("#pkTable tbody").innerHTML = pkRows.map((r) => `<tr><td>${esc(r.name || r.gtin)}<div class="small muted">${esc(r.gtin)}</div></td><td>${esc(r.grade_band || "")}</td><td>${esc(r.subject || "")}</td><td>${r.qty}</td><td>${unitOf(r) != null ? money(unitOf(r)) : ""}${r.value_basis && r.fmv_unit == null ? `<div class="small muted">${esc(r.value_basis.replace(/ \(.*\)$/, ""))}</div>` : ""}</td><td>${money(r.value_est)}</td></tr>`).join("");
    loadRecentVisits();
  }
  $("#btnPrintPk").onclick = () => {
    const w = window.open("", "_blank");
    w.document.write(`<title>${esc(pkId)} packing list</title><style>body{font:13px system-ui;margin:24px}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ccc;padding:4px;text-align:left}h1{font-size:18px;color:#1b2a52}</style><h1>The Education Partnership — packing list ${esc(pkId)}</h1>${$("#pkHead").innerHTML}<table>${$("#pkTable").innerHTML}</table><p style="margin-top:40px">Received by: ______________________ &nbsp; Date: __________</p>`);
    w.document.close(); w.focus(); w.print();
  };
  $("#btnPkCsv").onclick = () => download(`${pkId}_packing_list.csv`, [["distribution_id", "barcode", "item", "grade_band", "subject", "qty", "unit_value", "value", "value_basis"]]
    .concat(pkRows.map((r) => [r.distribution_id, r.gtin, r.name, r.grade_band, r.subject, r.qty, unitOf(r), r.value_est, r.value_basis || r.fmv_status])));
  $("#btnPkClose").onclick = () => { $("#distPacking").classList.add("hidden"); $("#distStart").classList.remove("hidden"); };

  async function loadRecentVisits() {
    const [s, d] = await Promise.all([
      sb.from("v_distribution_summary").select("*").order("distributed_on", { ascending: false }).order("distribution_id", { ascending: false }).limit(50),
      sb.from("distributions").select("distribution_id, program_code").order("distributed_on", { ascending: false }).limit(200)]);
    if (s.error) return fail(s.error);
    const prog = Object.fromEntries((d.data || []).map((x) => [x.distribution_id, x.program_code]));
    const pname = (c) => programs.find((x) => x.program_code === c)?.name || c || "";
    $("#dRecent tbody").innerHTML = s.data.map((r) => `<tr><td>${esc(r.distribution_id)}</td><td>${r.distributed_on}</td><td>${esc(pname(prog[r.distribution_id]))}</td><td>${esc(r.partner)}</td><td>${num(r.units)}</td><td>${num(r.titles)}</td><td>${money(r.value_est)}</td><td>${r.students_served ?? ""}</td>
      <td><button class="btn sec small" data-open="${esc(r.distribution_id)}" style="padding:3px 8px">Open</button> <button class="btn sec small" data-pk="${esc(r.distribution_id)}" style="padding:3px 8px">Packing list</button></td></tr>`).join("");
    $("#dRecent tbody").querySelectorAll("[data-pk]").forEach((b) => (b.onclick = () => showPacking(b.dataset.pk)));
    $("#dRecent tbody").querySelectorAll("[data-open]").forEach((b) => (b.onclick = async () => {
      const { data, error } = await sb.from("distributions").select("*").eq("distribution_id", b.dataset.open).single(); if (error) return fail(error); openVisit(data); }));
  }

  // ------------------------------------------------------------ programs tab
  async function loadPrograms() {
    const s = await sb.from("v_program_summary").select("*").order("sort_order");
    if (s.error) return fail(s.error);
    $("#pgTable tbody").innerHTML = s.data.map((r) => `<tr><td><b>${esc(r.name)}</b><div class="small muted">${esc(r.program_code)}</div></td><td>${num(r.donations)}</td><td>${num(r.units_received)}</td><td>${num(r.units_distributed)}</td><td>${num(r.units_on_hand)}</td><td>${money(r.value_received_est)}</td><td>${num(r.visits)}</td><td>${num(r.partners_served)}</td><td>${num(r.students_served)}</td></tr>`).join("");
    loadBins(); loadGuide();
  }
  async function loadBins() {
    const { data, error } = await sb.from("v_program_stock_by_bin").select("*").eq("program_code", $("#pgPick").value).order("grade_band").order("subject");
    if (error) return fail(error);
    $("#pgBins tbody").innerHTML = data.map((r) => `<tr><td>${esc(r.grade_band)}</td><td>${esc(r.subject)}</td><td>${num(r.titles)}</td><td>${num(r.received)}</td><td>${num(r.distributed)}</td><td>${num(r.on_hand)}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">Nothing received in this program yet.</td></tr>`;
  }
  $("#pgPick").onchange = () => loadBins();
  async function loadGuide() {
    const { data, error } = await sb.from("value_guide").select("*").order("unit_value", { ascending: false });
    if (error) return fail(error);
    $("#vgStaffNote").textContent = isStaff ? "Staff: change an amount and press Save; tick Approved once finance signs off." : "";
    $("#vgTable tbody").innerHTML = data.map((r) => `<tr><td>${esc(r.label)}</td>
      <td>${isStaff ? `<input type="number" step="0.01" min="0" value="${r.unit_value}" data-v="${esc(r.guide_code)}" style="width:90px">` : money(r.unit_value)}</td>
      <td>${isStaff ? `<label style="display:inline;font-size:14px"><input type="checkbox" data-a="${esc(r.guide_code)}" ${r.status === "approved" ? "checked" : ""} style="width:auto"> Approved</label>` : `<span class="pill ${r.status === "approved" ? "ok" : ""}">${esc(r.status)}</span>`}
        ${r.approved_by ? `<div class="small muted">${esc(r.approved_by)}</div>` : ""}</td>
      <td class="small muted">${esc(r.sources || "")}</td><td>${isStaff ? `<button class="btn sec small" data-save="${esc(r.guide_code)}" style="padding:3px 8px">Save</button>` : ""}</td></tr>`).join("");
    $("#vgTable tbody").querySelectorAll("[data-save]").forEach((b) => (b.onclick = async () => {
      const code = b.dataset.save, approved = $(`[data-a="${code}"]`).checked;
      const up = await sb.from("value_guide").update({ unit_value: +$(`[data-v="${code}"]`).value, status: approved ? "approved" : "TEP estimate",
        approved_by: approved ? user.email : null, approved_at: approved ? new Date().toISOString() : null, updated_at: new Date().toISOString() }).eq("guide_code", code);
      if (up.error) return fail(up.error); toast("Saved", "ok"); loadGuide();
    }));
  }

  // ------------------------------------------------------------ hook into navigation
  const baseView = window.onCatalogView;
  window.onCatalogView = (v) => {
    if (baseView) baseView(v);
    if (v === "distribute") {
      loadRecentVisits();
      const open = localStorage.getItem("gik.visit");
      if (open && !visit) sb.from("distributions").select("*").eq("distribution_id", open).maybeSingle().then(({ data }) => data && openVisit(data));
      else if (visit) setTimeout(() => $("#dScan").focus(), 50);
    }
    if (v === "programs") loadPrograms();
  };
  if (user) loadProgramRefs();
})();
