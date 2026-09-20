/* Station page: live LoRa traffic + in-flight notice. Polls live.json; no dependencies.
   The page is complete without this script; it only keeps it current. */
(function () {
  "use strict";

  var wrap = document.getElementById("linkbar-wrap");
  if (!wrap || !window.fetch) return;

  var POLL_MS = 1500;
  var HEX_PREVIEW_BYTES = 24;          // mirror of routes.py
  var url = wrap.getAttribute("data-live-url");
  var bar = document.getElementById("linkbar");
  var list = document.getElementById("timeline");     // summary tab only
  var status = document.getElementById("live-status");
  var skew = 0;            // server clock minus browser clock, seconds
  var offsetMin = 0;       // station UTC offset, for absolute times
  var timer = null;
  var stopped = false;
  var bannerKey = "";

  // --- small helpers ----------------------------------------------------------

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function nowS() { return Date.now() / 1000 + skew; }

  function ago(ts) {
    var d = Math.max(0, Math.round(nowS() - ts));
    if (d < 60) return "hace " + d + " s";
    if (d < 3600) return "hace " + Math.floor(d / 60) + " min";
    if (d < 86400) return "hace " + Math.floor(d / 3600) + " h";
    return "hace " + Math.floor(d / 86400) + " d";
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function stationDate(ts) { return new Date((ts + offsetMin * 60) * 1000); }

  function clock(ts) {
    var d = stationDate(ts);
    return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
  }

  function dateTime(ts) {
    var d = stationDate(ts);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  function esNum(v) { return v.toFixed(3).replace(".", ","); }

  function hexBytes(hex) {
    var pairs = hex.match(/.{1,2}/g) || [];
    var more = pairs.length > HEX_PREVIEW_BYTES ? " …" : "";
    return pairs.slice(0, HEX_PREVIEW_BYTES).join(" ") + more;
  }

  function replay(node, cls) {
    node.classList.remove(cls);
    void node.offsetWidth;             // restart the CSS animation
    node.classList.add(cls);
  }

  // --- timeline ---------------------------------------------------------------

  function taStrip(ta) {
    var span = (ta.max - ta.min) || 1;
    var strip = el("div", "ta-strip");
    var bars = el("div", "ta-bars");
    bars.setAttribute("role", "img");
    bars.setAttribute("aria-label", "Temperatura del aire: " + ta.past.length +
      " h anteriores y " + ta.future.length + " h de previsión, de " + ta.min +
      " a " + ta.max + " °C");
    var axis = el("div", "ta-axis");
    [["ta-past", ta.past, " h anteriores"], ["ta-future", ta.future, " h de previsión"]]
      .forEach(function (part) {
        var seg = el("div", "ta-seg " + part[0]);
        seg.style.flex = part[1].length;
        part[1].forEach(function (v) {
          var b = el("i");
          b.style.height = ((v - ta.min) / span * 82 + 18).toFixed(1) + "%";
          seg.appendChild(b);
        });
        bars.appendChild(seg);
        var label = el("span", null, part[1].length + part[2]);
        label.style.flex = part[1].length;
        axis.appendChild(label);
      });
    var cap = el("div", "ta-cap", "Temperatura del aire · Open-Meteo ");
    cap.appendChild(el("span", "ta-range", "mín " + ta.min + " °C · máx " + ta.max + " °C"));
    strip.appendChild(bars);
    strip.appendChild(axis);
    strip.appendChild(cap);
    return strip;
  }

  // Same markup as the frame_row macro of station.html.
  function buildRow(r) {
    var up = r.dir === "up";
    var kind = up ? r.type : r.kind;
    var li = el("li", "tl-row tl-" + r.dir);
    li.setAttribute("data-key", r.key);
    var glyph = el("span", "tl-glyph", up ? "↑" : "↓");
    glyph.setAttribute("aria-hidden", "true");
    li.appendChild(glyph);

    var body = el("div", "tl-body");
    var head = el("div", "tl-head");
    head.appendChild(el("span", "tl-route", up ? "estación → nube" : "nube → estación"));
    head.appendChild(el("span", "chip chip-" + kind, kind));
    head.appendChild(el("span", "tl-bytes", r.bytes + " B"));
    if (!up) head.appendChild(el("span", "chip st-" + r.state + " tl-state", r.label));
    var t = el("time", "tl-time");
    head.appendChild(t);
    body.appendChild(head);
    body.appendChild(el("div", "tl-summary", r.summary));
    if (r.ta) body.appendChild(taStrip(r.ta));
    var hex = el("div", "tl-hex mono", hexBytes(r.payload_hex));
    hex.title = r.payload_hex;
    body.appendChild(hex);
    li.appendChild(body);
    return li;
  }

  function merge(data) {
    var rows = data.uplinks.map(function (u) {
      return Object.assign({ dir: "up", key: "u" + u.id, at_s: u.ts_s }, u);
    }).concat(data.downlinks.map(function (d) {
      return Object.assign({ dir: "down", key: "d" + d.id, at_s: d.delivered_s || d.ts_s }, d);
    }));
    // Newest first; on a tie the downlink sits above the uplink it answered.
    rows.sort(function (a, b) {
      return (b.at_s - a.at_s) || ((b.dir === "down") - (a.dir === "down")) ||
        ((b.id || 0) - (a.id || 0));
    });
    return rows;
  }

  // Drop the one-shot classes once played, or moving the row would replay them.
  if (list) {
    list.addEventListener("animationend", function (ev) {
      if (ev.animationName !== "tl-glow") return;
      var li = ev.target.closest(".tl-row");
      if (li) li.classList.remove("is-new", "is-changed");
    });
  }

  function syncTimeline(data) {
    if (!list) return;
    var rows = merge(data);
    var known = {};
    Array.prototype.forEach.call(list.querySelectorAll(".tl-row"), function (li) {
      known[li.getAttribute("data-key")] = li;
    });
    var empty = list.querySelector(".tl-empty");
    if (empty && rows.length) empty.remove();

    var cursor = list.firstElementChild;
    rows.forEach(function (r) {
      var li = known[r.key];
      if (li) {
        delete known[r.key];
        var chip = li.querySelector(".tl-state");
        if (chip && chip.textContent !== r.label) {     // scheduled -> delivered -> applied
          chip.className = "chip st-" + r.state + " tl-state";
          chip.textContent = r.label;
          replay(li, "is-changed");
        }
      } else {
        li = buildRow(r);
        li.classList.add("is-new");
      }
      var t = li.querySelector(".tl-time");
      t.setAttribute("data-ts", r.at_s);
      if (li === cursor) cursor = cursor.nextElementSibling;
      else list.insertBefore(li, cursor);
    });
    Object.keys(known).forEach(function (k) { known[k].remove(); });   // scrolled out
    tick();
  }

  function tick() {
    Array.prototype.forEach.call(document.querySelectorAll(".tl-time[data-ts]"), function (t) {
      var ts = Number(t.getAttribute("data-ts"));
      t.textContent = ago(ts);
      t.title = clock(ts);
    });
    // Header stamps: the server prints the absolute instant so two renders of a
    // page are identical; here they become "hace 4 min" and stay current.
    Array.prototype.forEach.call(document.querySelectorAll(".rel-time[data-ts]"), function (t) {
      var ts = Number(t.getAttribute("data-ts"));
      if (!ts) return;
      t.textContent = ago(ts);
      t.title = dateTime(ts);
    });
  }

  // --- in-flight notice -------------------------------------------------------

  function syncBanner(b) {
    if (!b) {
      wrap.classList.remove("is-open");
      bannerKey = "";
      return;
    }
    var key = b.state + ":" + b.key;
    bar.querySelector(".lb-title").textContent = b.title;
    bar.querySelector(".lb-detail").textContent = b.detail;
    bar.querySelector(".lb-meta").textContent = b.meta || "";
    if (key !== bannerKey) {
      bannerKey = key;
      bar.className = "linkbar is-" + b.state;
    }
    wrap.classList.add("is-open");
  }

  // --- status card ------------------------------------------------------------

  function syncStation(data) {
    var last = document.getElementById("live-last-uplink");
    if (last && data.station.last_uplink_at) last.textContent = dateTime(data.station.last_uplink_at);

    var pending = document.getElementById("live-pending");
    if (pending) {
      pending.hidden = !data.pending;
      pending.textContent = data.pending + " en cola";
    }

    var box = document.getElementById("live-onboard");
    var f = data.forecast;
    if (box && f && f.source === "station") {
      var value = document.getElementById("live-onboard-value");
      var text = esNum(f.hs30_min);
      if (value.textContent !== text || box.hidden) {
        value.textContent = text;
        box.hidden = false;
        replay(box, "is-changed");
      }
      document.getElementById("live-onboard-at").textContent = dateTime(f.run_ts_s);
      var none = document.getElementById("live-no-forecast");
      if (none) none.hidden = true;
    }
  }

  function setStatus(on, label) {
    if (!status) return;
    status.hidden = false;
    status.classList.toggle("is-off", !on);
    document.getElementById("live-label").textContent = label;
  }

  // --- polling ----------------------------------------------------------------

  function schedule() {
    clearTimeout(timer);
    if (!stopped && !document.hidden) timer = setTimeout(poll, POLL_MS);
  }

  function poll() {
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (resp.status === 401) {
          stopped = true;
          setStatus(false, "sesión caducada: recarga la página");
          return null;
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data) return;
        skew = data.now_s - Date.now() / 1000;
        offsetMin = data.station.utc_offset_min || 0;
        syncTimeline(data);
        syncBanner(data.banner);
        syncStation(data);
        setStatus(true, "en directo");
      })
      .catch(function () { setStatus(false, "sin conexión: reintentando"); })
      .then(schedule);
  }

  document.addEventListener("visibilitychange", function () {
    clearTimeout(timer);
    if (!document.hidden && !stopped) poll();      // catch up at once
  });
  setInterval(tick, 1000);
  poll();
})();
