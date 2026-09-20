/* Table enhancement: sort on a header, filter with a box, and a shared time range.
   The table is already complete server-side; this only reorders and hides rows.
   The range control is the one filter row of the page -- it scopes the table and
   announces itself so the charts follow the same slice. */
(function () {
  "use strict";

  var COLLATOR = new Intl.Collator("es", { numeric: true, sensitivity: "base" });
  var minTs = 0;                       // 0 = no time filter

  function bodyRows(table) {
    return Array.prototype.filter.call(
      table.tBodies[0] ? table.tBodies[0].rows : [],
      function (tr) { return !tr.querySelector("td[colspan]"); });
  }

  function cellText(tr, index) {
    var td = tr.cells[index];
    return td ? td.textContent.trim() : "";
  }

  function asNumber(text) {
    // "-121 / -11.5" sorts by its first number; an em dash sorts last.
    var m = text.replace(",", ".").match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : Number.NEGATIVE_INFINITY;
  }

  // --- filtering ---------------------------------------------------------------

  function applyFilters(table) {
    var needle = (table.__needle || "").toLowerCase();
    var shown = 0;
    bodyRows(table).forEach(function (tr) {
      var ts = Number(tr.getAttribute("data-ts") || 0);
      var inRange = !minTs || !ts || ts >= minTs;
      var hit = !needle || tr.textContent.toLowerCase().indexOf(needle) !== -1;
      var visible = inRange && hit;
      tr.classList.toggle("row-hidden", !visible);
      if (visible) shown += 1;
    });
    var total = bodyRows(table).length;
    var note = document.querySelector('[data-count-for="' + table.id + '"]');
    if (note) {
      note.textContent = shown === total
        ? total + (total === 1 ? " fila" : " filas")
        : shown + " de " + total;
    }
    noMatches(table, total > 0 && shown === 0);
  }

  /* Filtering everything away must say so, not leave an empty frame. */
  function noMatches(table, show) {
    var row = table.__noMatch;
    if (!row) {
      if (!show) return;
      row = table.insertRow(-1);
      var cell = row.insertCell(0);
      cell.colSpan = table.rows[0].cells.length;
      cell.className = "wrap";
      cell.innerHTML = '<div class="empty"><strong>Sin coincidencias</strong>' +
        "<p>Ninguna fila cumple el filtro.</p></div>";
      table.__noMatch = row;
    }
    row.classList.toggle("row-hidden", !show);
  }

  // --- sorting ------------------------------------------------------------------

  function sortBy(table, th) {
    var head = th.parentElement;
    var index = Array.prototype.indexOf.call(head.cells, th);
    var numeric = th.getAttribute("data-sort") === "number";
    var asc = th.getAttribute("aria-sort") !== "ascending";

    Array.prototype.forEach.call(head.cells, function (c) {
      if (c !== th) c.removeAttribute("aria-sort");
    });
    th.setAttribute("aria-sort", asc ? "ascending" : "descending");
    var caret = th.querySelector(".caret");
    if (caret) caret.textContent = asc ? "↑" : "↓";

    var rows = bodyRows(table);
    rows.sort(function (a, b) {
      var x = cellText(a, index), y = cellText(b, index);
      var r = numeric ? asNumber(x) - asNumber(y) : COLLATOR.compare(x, y);
      return asc ? r : -r;
    });
    var body = table.tBodies[0];
    rows.forEach(function (tr) { body.appendChild(tr); });
    if (table.__noMatch) body.appendChild(table.__noMatch);   // stays last
  }

  // --- wiring ---------------------------------------------------------------------

  document.querySelectorAll("table[data-table]").forEach(function (table) {
    table.querySelectorAll("th.sortable").forEach(function (th) {
      th.tabIndex = 0;
      th.addEventListener("click", function () { sortBy(table, th); });
      th.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          sortBy(table, th);
        }
      });
    });
    applyFilters(table);
  });

  document.querySelectorAll("[data-search]").forEach(function (input) {
    var table = document.getElementById(input.getAttribute("data-search"));
    if (!table) return;
    input.addEventListener("input", function () {
      table.__needle = input.value.trim();
      applyFilters(table);
    });
  });

  // --- the range control: one row, scoping everything under it ---------------------

  var group = document.querySelector("[data-range]");
  if (group) {
    group.addEventListener("click", function (ev) {
      var button = ev.target.closest("button[data-hours]");
      if (!button) return;
      group.querySelectorAll("button").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b === button));
      });
      var hours = Number(button.getAttribute("data-hours"));
      var newest = 0;
      document.querySelectorAll("tr[data-ts]").forEach(function (tr) {
        newest = Math.max(newest, Number(tr.getAttribute("data-ts")));
      });
      minTs = hours && newest ? newest - (hours - 1) * 3600 : 0;
      document.querySelectorAll("table[data-table]").forEach(applyFilters);
      document.dispatchEvent(new CustomEvent("savia:range", { detail: { minTs: minTs } }));
    });
  }
})();
