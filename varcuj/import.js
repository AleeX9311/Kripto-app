/* Local bank-statement parser. Nothing leaves the phone. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof root !== "undefined") root.VarcujImport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const KEYWORDS = [
    [/mercator|spar|hofer|lidl|tus|eurospin|jager/, "hrana"],
    [/petrol|omv|mol/, "gorivo"],
    [/netflix|spotify|hbo|voyo|deezer/, "zabava"],
    [/telekom|a1|telemach|t2/, "poloznice"],
    [/elektro|energija|gen-i|geni|petrol plin|komunal|snaga|vodovod|toplarna/, "poloznice"],
    [/najemnina|stanovanjsk/, "stanovanje"],
    [/zavarovalnica|vzajemna|triglav|lekarna|zdravstven/, "zdravje"],
    [/\bh&m\b|\bhm\b|zara|reserved|aboutyou|about you/, "oblacila"]
  ];

  function pad(n) { return String(n).padStart(2, "0"); }
  function norm(value) {
    return String(value || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "");
  }
  function decodeXml(value) {
    return String(value || "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, "\"").replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)));
  }
  function decodeBytes(bytes) {
    const utf8 = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    if (!utf8.includes("\uFFFD")) return { text: utf8.replace(/^\uFEFF/, ""), encoding: "utf-8" };
    return { text: new TextDecoder("windows-1250").decode(bytes).replace(/^\uFEFF/, ""), encoding: "windows-1250" };
  }
  function splitCsv(line, delimiter) {
    const out = [];
    let cur = "", quote = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (quote) {
        if (ch === "\"" && line[i + 1] === "\"") { cur += "\""; i++; }
        else if (ch === "\"") quote = false;
        else cur += ch;
      } else if (ch === "\"") quote = true;
      else if (ch === delimiter) { out.push(cur); cur = ""; }
      else cur += ch;
    }
    out.push(cur);
    return out.map(cell => cell.trim());
  }
  function detectDelimiter(text) {
    const lines = text.split(/\r?\n/).filter(line => line.trim()).slice(0, 6);
    const score = (delimiter) => lines.reduce((sum, line) => sum + splitCsv(line, delimiter).length, 0);
    return score(";") >= score(",") ? ";" : ",";
  }
  function parseMoney(value) {
    let raw = String(value == null ? "" : value).trim();
    if (!raw || raw === "-" || raw === "0" || raw === "0,00" || raw === "0.00") return raw === "" ? null : 0;
    let sign = 1;
    if (/^\(.*\)$/.test(raw)) { sign = -1; raw = raw.slice(1, -1); }
    if (raw.startsWith("+")) raw = raw.slice(1);
    if (raw.startsWith("-")) { sign = -1; raw = raw.slice(1); }
    raw = raw.replace(/\s/g, "").replace(/eur|€/ig, "");
    if (raw.includes(",") && raw.includes(".")) {
      if (raw.lastIndexOf(",") > raw.lastIndexOf(".")) raw = raw.replace(/\./g, "").replace(",", ".");
      else raw = raw.replace(/,/g, "");
    } else if (raw.includes(",")) raw = raw.replace(",", ".");
    const num = Number(raw);
    return Number.isFinite(num) ? Math.round(sign * num * 100) / 100 : null;
  }
  function excelSerial(value) {
    if (!/^\d{5}(\.\d+)?$/.test(String(value).trim())) return null;
    const num = Number(value);
    if (num < 30000 || num > 70000) return null;
    const date = new Date(Date.UTC(1899, 11, 30) + num * 86400000);
    return date.getUTCFullYear() + "-" + pad(date.getUTCMonth() + 1) + "-" + pad(date.getUTCDate());
  }
  function parseDate(value) {
    const raw = String(value || "").trim();
    let match = raw.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})/);
    if (match) return match[3] + "-" + pad(match[2]) + "-" + pad(match[1]);
    match = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (match) return match[1] + "-" + match[2] + "-" + match[3];
    match = raw.match(/^(\d{1,2})\.(\d{1,2})\.(\d{2})$/);
    if (match) return "20" + match[3] + "-" + pad(match[2]) + "-" + pad(match[1]);
    match = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (match) return match[3] + "-" + pad(match[2]) + "-" + pad(match[1]);
    return excelSerial(raw);
  }
  function guess(headers) {
    const names = headers.map(norm);
    const find = (...keys) => names.findIndex(name => keys.some(key => name === key || name.includes(key)));
    const date = find("datumknjizenja", "datumtransakcije", "datumvalute", "datum");
    const debit = find("vbreme", "breme", "obremenitev", "odliv", "debit");
    const credit = find("vdobro", "dobro", "odobritev", "priliv", "vprid", "credit");
    let amount = -1;
    names.forEach((name, index) => {
      if (amount >= 0) return;
      if (index === debit || index === credit) return;
      if (name === "znesek" || name === "amount" || name.includes("znesekv")) amount = index;
    });
    const desc = find("namen", "opisprometa", "opis", "prejemniknalogodajalec", "prejemnik", "nazivpartnerja", "partner", "placnik");
    const sure = date >= 0 && desc >= 0 && (amount >= 0 || debit >= 0 || credit >= 0);
    return { date, desc, amount, debit, credit, sure };
  }
  function tableFromText(text) {
    if (/<table[\s>]/i.test(text)) return htmlTable(text);
    if (/<Workbook[\s>]|<worksheet[\s>]/i.test(text)) return xmlTable(text);
    const delimiter = detectDelimiter(text);
    const rows = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => splitCsv(line, delimiter));
    return { rows, delimiter };
  }
  function htmlTable(text) {
    const rows = [];
    const parts = text.match(/<tr[\s\S]*?<\/tr>/gi) || [];
    parts.forEach(row => {
      const cells = [...row.matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)].map(match => decodeXml(match[1].replace(/<[^>]+>/g, "")).trim());
      if (cells.some(Boolean)) rows.push(cells);
    });
    return { rows, delimiter: "html" };
  }
  function xmlTable(text) {
    const rows = [];
    const parts = text.match(/<Row[\s\S]*?<\/Row>/gi) || [];
    parts.forEach(row => {
      const cells = [...row.matchAll(/<Data[^>]*>([\s\S]*?)<\/Data>/gi)].map(match => decodeXml(match[1]).trim());
      if (cells.some(Boolean)) rows.push(cells);
    });
    return { rows, delimiter: "xml" };
  }
  function colIndex(letters) {
    return letters.split("").reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  }
  function sharedStrings(xml) {
    const out = [];
    xml.split(/<si[\s>]/).slice(1).forEach(part => {
      out.push([...part.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(match => decodeXml(match[1])).join(""));
    });
    return out;
  }
  function sheetRows(xml, strings) {
    const rows = [];
    const rowRe = /<row\b[^>]*>([\s\S]*?)<\/row>/g;
    let row;
    while ((row = rowRe.exec(xml))) {
      const indexed = {};
      const cellRe = /<c\b([^>]*)>([\s\S]*?)<\/c>/g;
      let cell;
      while ((cell = cellRe.exec(row[1]))) {
        const ref = (cell[1].match(/\br="([A-Z]+)/) || [])[1] || "";
        const kind = (cell[1].match(/\bt="([^"]+)"/) || [])[1] || "";
        const value = (cell[2].match(/<v>([\s\S]*?)<\/v>/) || [])[1];
        let text = value || "";
        if (kind === "s") text = strings[Number(value)] || "";
        if (kind === "inlineStr") text = [...cell[2].matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(match => decodeXml(match[1])).join("");
        if (ref) indexed[colIndex(ref)] = decodeXml(text);
      }
      const max = Math.max(-1, ...Object.keys(indexed).map(Number));
      const arr = [];
      for (let i = 0; i <= max; i++) arr.push(indexed[i] || "");
      if (arr.some(Boolean)) rows.push(arr);
    }
    return rows;
  }
  async function unzip(buffer) {
    const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 66000); i--) {
      if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("ni zip");
    const count = view.getUint16(eocd + 8, true);
    let ptr = view.getUint32(eocd + 16, true);
    const files = {};
    for (let n = 0; n < count; n++) {
      if (view.getUint32(ptr, true) !== 0x02014b50) break;
      const method = view.getUint16(ptr + 10, true);
      const compSize = view.getUint32(ptr + 20, true);
      const nameLen = view.getUint16(ptr + 28, true);
      const extra = view.getUint16(ptr + 30, true);
      const comment = view.getUint16(ptr + 32, true);
      const localOff = view.getUint32(ptr + 42, true);
      const name = new TextDecoder().decode(bytes.subarray(ptr + 46, ptr + 46 + nameLen));
      const localName = view.getUint16(localOff + 26, true);
      const localExtra = view.getUint16(localOff + 28, true);
      const start = localOff + 30 + localName + localExtra;
      const comp = bytes.subarray(start, start + compSize);
      let raw = comp;
      if (method === 8) {
        const stream = new Blob([comp]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
        raw = new Uint8Array(await new Response(stream).arrayBuffer());
      }
      files[name] = new TextDecoder().decode(raw);
      ptr += 46 + nameLen + extra + comment;
    }
    return files;
  }
  function analyze(rows) {
    const headerIndex = rows.findIndex(row => row.some(cell => /datum|znesek|breme|dobro|opis|namen/i.test(cell)));
    const headers = headerIndex >= 0 ? rows[headerIndex] : rows[0].map((_, i) => "Stolpec " + (i + 1));
    const body = rows.slice(headerIndex >= 0 ? headerIndex + 1 : 0);
    const mapping = guess(headers);
    return { headers, body, mapping, signature: headers.map(norm).join("|"), sure: mapping.sure && headerIndex >= 0 };
  }
  function applyRow(cells, mapping) {
    if (mapping.date < 0) return null;
    const date = parseDate(cells[mapping.date]);
    const desc = mapping.desc >= 0 ? String(cells[mapping.desc] || "").trim() : "";
    let amount = null;
    const debit = mapping.debit >= 0 ? parseMoney(cells[mapping.debit]) : null;
    const credit = mapping.credit >= 0 ? parseMoney(cells[mapping.credit]) : null;
    if (debit) amount = -Math.abs(debit);
    else if (credit) amount = Math.abs(credit);
    else if (mapping.amount >= 0) amount = parseMoney(cells[mapping.amount]);
    if (!date || amount == null || amount === 0) return null;
    return { date, desc, amount, abs: Math.abs(amount), type: amount < 0 ? "expense" : "income" };
  }
  function preview(body, mapping) {
    return body.map(cells => applyRow(cells, mapping)).filter(Boolean);
  }
  function categorize(desc, learned, categories) {
    const folded = norm(desc);
    const known = (learned || []).find(rule => folded.includes(norm(rule.merchant)));
    if (known) return known.categoryId;
    const hit = KEYWORDS.find(([pattern]) => pattern.test(folded));
    if (!hit) return "";
    const found = (categories || []).find(item => norm(item.name).includes(hit[1]));
    return found ? found.id : "";
  }
  function fingerprint(tx) { return tx.date + "|" + tx.amount.toFixed(2) + "|" + norm(tx.desc).slice(0, 48); }
  function suggestRecurring(txs) {
    const groups = new Map();
    txs.filter(tx => tx.type === "expense" && tx.desc).forEach(tx => {
      const key = norm(tx.desc).slice(0, 40) + "|" + tx.abs.toFixed(2);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(tx);
    });
    const out = [];
    groups.forEach(list => {
      if (list.length < 2) return;
      const dates = list.map(tx => tx.date).sort();
      const gaps = [];
      for (let i = 1; i < dates.length; i++) gaps.push((new Date(dates[i]) - new Date(dates[i - 1])) / 86400000);
      const mid = gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)];
      const period = mid >= 25 && mid <= 35 ? "month" : mid >= 330 && mid <= 400 ? "year" : "";
      if (!period) return;
      const last = list[list.length - 1];
      out.push({ name: last.desc.slice(0, 42), amount: last.abs, period, next: period === "year" ? addYear(last.date) : addMonth(last.date) });
    });
    return out;
  }
  function addMonth(value) {
    const date = new Date(value + "T12:00:00");
    date.setMonth(date.getMonth() + 1);
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
  }
  function addYear(value) {
    const date = new Date(value + "T12:00:00");
    date.setFullYear(date.getFullYear() + 1);
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
  }
  async function parseBuffer(buffer) {
    const bytes = new Uint8Array(buffer);
    if (bytes[0] === 0x50 && bytes[1] === 0x4b) {
      const files = await unzip(bytes);
      const sheetName = Object.keys(files).find(name => /xl\/worksheets\/sheet1\.xml$/.test(name));
      const strings = sharedStrings(files["xl/sharedStrings.xml"] || "");
      const rows = sheetRows(files[sheetName] || "", strings);
      const analyzed = analyze(rows);
      return Object.assign({ encoding: "xlsx", delimiter: "xlsx" }, analyzed);
    }
    const decoded = decodeBytes(bytes);
    if (bytes[0] === 0xd0 && bytes[1] === 0xcf && !/<table|<Workbook/i.test(decoded.text)) {
      throw new Error("Te .xls datoteke ne znam prebrati. V Bankanetu izvozi CSV ali xlsx.");
    }
    const table = tableFromText(decoded.text);
    return Object.assign({ encoding: decoded.encoding, delimiter: table.delimiter }, analyze(table.rows));
  }
  return { parseBuffer, preview, categorize, fingerprint, suggestRecurring, guess, parseDate, parseMoney, norm };
});
