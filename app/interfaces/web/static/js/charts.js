/* Interactive charts. Progressive enhancement: every chart is already on the page
   as server-rendered SVG, and this file swaps in the ApexCharts twin -- same data,
   same hues, plus a crosshair, a shared readout and a time range.

   Two rules the panel keeps: soil water and air temperature never share a y-scale
   (one axis per measure, stacked panels on one time axis), and text never wears a
   series colour -- the mark beside it carries the identity. */
(function () {
  "use strict";

  if (!window.ApexCharts) return;

  var css = getComputedStyle(document.documentElement);
  var T = {};
  ["--viz-hs10", "--viz-hs30", "--viz-ta", "--line", "--line-soft", "--line-strong",
   "--muted", "--ink", "--surface", "--accent"].forEach(function (name) {
    T[name.slice(2)] = css.getPropertyValue(name).trim();
  });

  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var MAX_GAP_S = 3 * 3600;          // mirror of charts.py: a longer hole cuts the line
  var charts = {};

  // --- helpers ----------------------------------------------------------------

  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  /* Instants arrive already shifted into station-local time, so they are read
     back as UTC -- the same trick the `dt` template filter uses. */
  function stamp(ms) {
    var d = new Date(ms);
    return pad(d.getUTCDate()) + "/" + pad(d.getUTCMonth() + 1) + " " +
           pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  function fixed(v, n) { return v === null || v === undefined ? "—" : v.toFixed(n); }

  /* Hour labels for the time axis. Apex only honours tickAmount on a datetime
     axis when the labels come from a function, so this is also what keeps the
     axis to a handful of ticks instead of one every two hours. A tick on local
     midnight carries the date instead of a pair of zeroes, which is what tells a
     window spanning midnight apart; the caption under the chart names the full
     range, so the label stays stateless and cannot drift between draws. */
  function timeAxis(value) {
    var ms = Number(value);
    if (!isFinite(ms)) return "";
    var d = new Date(ms);
    if (d.getUTCHours() === 0 && d.getUTCMinutes() === 0) {
      return pad(d.getUTCDate()) + "/" + pad(d.getUTCMonth() + 1);
    }
    return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  /* The x positions every series on a panel shares: one per reading, plus a
     break wherever the readings skip more than MAX_GAP_S. A shared tooltip reads
     each series at the same index, so the series must be built on one grid --
     dropping a series' empty hours instead would slide it out of step with its
     neighbours and report the wrong hour. */
  function grid(rows) {
    var out = [];
    var prev = null;
    rows.forEach(function (r) {
      if (prev !== null && (r.t - prev) > MAX_GAP_S * 1000) {
        out.push({ t: prev + 1000, row: null });
      }
      out.push({ t: r.t, row: r });
      prev = r.t;
    });
    return out;
  }

  /* One field over that grid: null at a break and wherever the hour has no
     value, which is what cuts the line instead of bridging the hole. */
  function points(slots, key) {
    return slots.map(function (s) {
      var v = s.row ? s.row[key] : null;
      return { x: s.t, y: (v === null || v === undefined) ? null : v };
    });
  }

  function hasValues(series) {
    return series.some(function (p) { return p.y !== null; });
  }

  /* An hour whose neighbours are both empty has no line to be part of, so it
     gets its own marker -- otherwise a lone reading draws nothing at all. */
  function lonePoints(series, seriesIndex, color) {
    var out = [];
    series.forEach(function (p, i) {
      if (p.y === null) return;
      var before = i > 0 ? series[i - 1].y : null;
      var after = i < series.length - 1 ? series[i + 1].y : null;
      if (before === null && after === null) {
        out.push({
          seriesIndex: seriesIndex,
          dataPointIndex: i,
          size: 4,
          fillColor: color,
          strokeColor: T.surface,
          strokeWidth: 2,
        });
      }
    });
    return out;
  }

  function allLonePoints(series, colors) {
    return series.reduce(function (acc, s, idx) {
      return acc.concat(lonePoints(s.data, idx, colors[idx]));
    }, []);
  }

  var HOUR_MS = 3600000;
  var STEP_HOURS = [1, 2, 3, 6, 12, 24, 48];

  /* How many hour labels fit before they start colliding, from the space the
     chart actually has -- a phone gets four, a desktop card seven. */
  function maxTicks(box) {
    var width = (box && box.clientWidth) || 0;
    return width && width < 520 ? 4 : 7;
  }

  /* A time axis whose ticks land on whole hours. Apex spreads datetime ticks
     evenly across the raw range, which prints times like 20:50; widening the
     domain to the nearest multiple of the step puts every tick on the hour -- and
     on a local midnight often enough for the date labels to fall where they help. */
  function timeScale(first, last, ticks) {
    var step = STEP_HOURS[STEP_HOURS.length - 1] * HOUR_MS;
    for (var i = 0; i < STEP_HOURS.length; i += 1) {
      var candidate = STEP_HOURS[i] * HOUR_MS;
      var lo = Math.floor(first / candidate) * candidate;
      var hi = Math.ceil(last / candidate) * candidate;
      if ((hi - lo) / candidate <= ticks) { step = candidate; break; }
    }
    var min = Math.floor(first / step) * step;
    var max = Math.ceil(last / step) * step;
    if (max <= min) max = min + step;
    return { min: min, max: max, tickAmount: Math.round((max - min) / step) };
  }

  function last(series) {
    for (var i = series.length - 1; i >= 0; i -= 1) {
      if (series[i].y !== null) return series[i];
    }
    return null;
  }

  // --- tooltip: values lead, series names follow, keyed by a short stroke -------

  function tooltip(title, rows) {
    var html = '<div class="tip"><div class="tip-head">' + title + "</div>";
    rows.forEach(function (r) {
      html += '<div class="tip-row">' +
        '<i class="tip-key" style="background:' + r.color + '"></i>' +
        '<span class="tip-name">' + r.name + "</span>" +
        '<span class="tip-val">' + r.value + "</span></div>";
    });
    return html + "</div>";
  }

  function sharedTooltip(units, decimals) {
    return function (ctx) {
      var w = ctx.w, i = ctx.dataPointIndex;
      var rows = [];
      w.config.series.forEach(function (s, si) {
        var point = s.data[i];
        if (!point || point.y === null || point.y === undefined) return;
        rows.push({
          color: w.globals.colors[si],
          name: s.name,
          value: fixed(point.y, decimals) + " " + units,
        });
      });
      if (!rows.length) return "";
      var x = w.config.series[0].data[i];
      return tooltip(stamp(x.x), rows);
    };
  }

  // --- shared options -----------------------------------------------------------

  function base(id, group, height) {
    return {
      chart: {
        id: id,
        group: group,
        type: "line",
        height: height,
        fontFamily: "inherit",
        parentHeightOffset: 0,
        toolbar: { show: false },
        zoom: { enabled: false },
        animations: {
          enabled: !reduced,
          speed: 420,
          easing: "easeinout",
          animateGradually: { enabled: false },
          dynamicAnimation: { enabled: !reduced, speed: 260 },
        },
      },
      grid: {
        borderColor: T.line,
        strokeDashArray: 0,
        xaxis: { lines: { show: false } },
        yaxis: { lines: { show: true } },
        // Room on the right for the direct label riding the newest point.
        padding: { top: 6, right: 54, bottom: 0, left: 4 },
      },
      dataLabels: { enabled: false },
      legend: { show: false },
      // Apex dims a line's stroke with the fill opacity; the series hues are
      // validated at full strength, so they are drawn at full strength.
      fill: { opacity: 1 },
      stroke: { width: 2, curve: "straight", lineCap: "round" },
      markers: {
        size: 0,
        strokeWidth: 2,
        strokeColors: T.surface,
        hover: { size: 5, sizeOffset: 0 },
      },
      xaxis: {
        type: "datetime",
        axisBorder: { show: false },
        axisTicks: { color: T.line },
        tooltip: { enabled: false },
        crosshairs: {
          show: true,
          stroke: { color: T["line-strong"], width: 1, dashArray: 0 },
        },
        tickAmount: 6,
        labels: {
          datetimeUTC: true,
          formatter: timeAxis,
          style: { colors: T.muted, fontSize: "11.5px" },
          rotate: 0,
          hideOverlappingLabels: true,
        },
      },
      tooltip: { shared: true, intersect: false, followCursor: false },
    };
  }

  /* Direct label on the newest point of a series: a ringed marker plus the value
     in ink. When two series share a panel the upper one rides above its point and
     the lower one below, so a label never leaves its line. */
  function endLabel(series, seriesIndex, color, text, above) {
    var point = last(series);
    if (!point) return null;
    return {
      x: point.x,
      y: point.y,
      seriesIndex: seriesIndex,
      marker: { size: 4, fillColor: color, strokeColor: T.surface, strokeWidth: 2 },
      label: {
        text: text,
        offsetY: above ? -6 : 22,
        borderColor: T.line,
        borderRadius: 6,
        style: {
          background: T.surface,
          color: T.ink,
          fontSize: "11.5px",
          fontWeight: 600,
          padding: { left: 6, right: 6, top: 3, bottom: 3 },
        },
      },
    };
  }

  function show(slot) {
    var live = slot.querySelector(".chart-live");
    var fallback = slot.querySelector(".chart-fallback");
    if (live) live.hidden = false;
    if (fallback) fallback.hidden = true;
  }

  function ready(box) {
    requestAnimationFrame(function () { box.classList.add("is-ready"); });
  }

  // --- stored readings: soil water, with air temperature on its own panel --------

  function mountReadings() {
    var slot = document.querySelector('.chart-slot[data-chart="soil"]');
    var data = readJson("readings-data");
    if (!slot || !data || !data.rows.length) return;

    var soilBox = slot.querySelector('[data-role="soil"]');
    var taBox = slot.querySelector('[data-role="ta"]');
    var taTitle = slot.querySelector('[data-role="ta-title"]');

    function build(rows) {
      var slots = grid(rows);
      return {
        hs10: points(slots, "hs10"),
        hs30: points(slots, "hs30"),
        ta: points(slots, "ta"),
      };
    }

    var all = build(data.rows);
    var ticks = maxTicks(soilBox);
    var scale = timeScale(data.rows[0].t, data.rows[data.rows.length - 1].t, ticks);
    var hasTa = hasValues(all.ta);
    var shows = { hs10: hasValues(all.hs10), hs30: hasValues(all.hs30) };
    var soilSeries = [];
    if (shows.hs10) soilSeries.push({ name: "HS10 · 10 cm", data: all.hs10 });
    if (shows.hs30) soilSeries.push({ name: "HS30 · 30 cm", data: all.hs30 });
    if (!soilSeries.length && !hasTa) return;

    show(slot);

    if (soilSeries.length) {
      var colors = soilSeries.map(function (s) {
        return s.name.indexOf("HS10") === 0 ? T["viz-hs10"] : T["viz-hs30"];
      });
      var opts = base("soil", "readings", 260);
      opts.series = soilSeries;
      opts.colors = colors;
      opts.yaxis = {
        tickAmount: 4,
        labels: {
          style: { colors: T.muted, fontSize: "11.5px" },
          formatter: function (v) { return v.toFixed(2); },
        },
      };
      opts.xaxis.labels.show = !hasTa;      // one time axis, on the lowest panel
      opts.xaxis.min = scale.min;
      opts.xaxis.max = scale.max;
      opts.xaxis.tickAmount = scale.tickAmount;
      opts.markers.discrete = allLonePoints(soilSeries, colors);
      opts.annotations = { points: soilEndLabels(soilSeries, colors) };
      opts.tooltip.custom = sharedTooltip("VWC", 3);
      charts.soil = new ApexCharts(soilBox, opts);
      charts.soil.render().then(function () { ready(soilBox); });
    } else {
      soilBox.hidden = true;
    }

    if (hasTa) {
      if (taTitle) taTitle.hidden = false;
      var taOpts = base("ta", "readings", 150);
      taOpts.chart.type = "area";
      taOpts.xaxis.min = scale.min;
      taOpts.xaxis.max = scale.max;
      taOpts.xaxis.tickAmount = scale.tickAmount;
      taOpts.series = [{ name: "Temperatura del aire", data: all.ta }];
      taOpts.colors = [T["viz-ta"]];
      taOpts.fill = {
        type: "gradient",
        gradient: { shadeIntensity: 0, opacityFrom: 0.18, opacityTo: 0.02, stops: [0, 100] },
      };
      taOpts.yaxis = {
        tickAmount: 3,
        labels: {
          style: { colors: T.muted, fontSize: "11.5px" },
          formatter: function (v) { return v.toFixed(0); },
        },
      };
      taOpts.markers.discrete = lonePoints(all.ta, 0, T["viz-ta"]);
      taOpts.annotations = {
        points: [endLabel(all.ta, 0, T["viz-ta"], fixed(last(all.ta).y, 1) + " °C", true)]
          .filter(Boolean),
      };
      taOpts.tooltip.custom = sharedTooltip("°C", 1);
      taBox.hidden = false;
      charts.ta = new ApexCharts(taBox, taOpts);
      charts.ta.render().then(function () { ready(taBox); });
    }

    // The range control scopes the charts exactly as it scopes the table.
    document.addEventListener("savia:range", function (ev) {
      // The range is a UTC instant (what the table rows carry); the points are
      // on the station-local scale, so it is shifted the same way before use.
      var shift = (data.offset_min || 0) * 60000;
      var floor = ev.detail.minTs ? ev.detail.minTs * 1000 + shift : 0;
      var rows = data.rows.filter(function (r) { return !floor || r.t >= floor; });
      if (!rows.length) return;
      var cut = build(rows);
      var cutScale = timeScale(rows[0].t, rows[rows.length - 1].t, ticks);
      if (charts.soil) {
        // The same series in the same order: colour follows the entity, never
        // the row number, so a narrower window never repaints the survivors.
        var next = [];
        if (shows.hs10) next.push({ name: "HS10 · 10 cm", data: cut.hs10 });
        if (shows.hs30) next.push({ name: "HS30 · 30 cm", data: cut.hs30 });
        charts.soil.updateOptions({
          series: next,
          xaxis: { min: cutScale.min, max: cutScale.max, tickAmount: cutScale.tickAmount },
          markers: { discrete: allLonePoints(next, charts.soil.w.globals.colors) },
          annotations: { points: soilEndLabels(next, charts.soil.w.globals.colors) },
        }, false, !reduced);
      }
      if (charts.ta) {
        charts.ta.updateOptions({
          series: [{ name: "Temperatura del aire", data: cut.ta }],
          xaxis: { min: cutScale.min, max: cutScale.max, tickAmount: cutScale.tickAmount },
          markers: { discrete: lonePoints(cut.ta, 0, T["viz-ta"]) },
        }, false, !reduced);
      }
      var label = document.querySelector("[data-range-label]");
      if (label) label.textContent = stamp(rows[0].t) + " → " + stamp(rows[rows.length - 1].t);
    });
  }

  function soilEndLabels(series, colors) {
    var tips = series.map(function (s) { return last(s.data); });
    // The series whose newest point sits higher takes the label above its dot.
    var top = 0;
    tips.forEach(function (p, idx) {
      if (p && (!tips[top] || p.y > tips[top].y)) top = idx;
    });
    return series.map(function (s, idx) {
      return endLabel(s.data, idx, colors[idx], fixed(tips[idx] && tips[idx].y, 3),
                      idx === top);
    }).filter(Boolean);
  }

  // --- stored inference: the 24 h HS30 forecast ----------------------------------

  function mountForecast() {
    var slot = document.querySelector('.chart-slot[data-chart="forecast"]');
    var data = readJson("forecast-data");
    if (!slot || !data || !data.rows.length) return;

    var box = slot.querySelector('[data-role="forecast"]');
    var series = data.rows.map(function (r) { return { x: r.t, y: r.hs30 }; });
    var lowest = series.reduce(function (a, b) { return b.y < a.y ? b : a; }, series[0]);

    show(slot);
    var opts = base("forecast", undefined, 230);
    opts.chart.type = "area";
    opts.series = [{ name: "HS30 previsto", data: series }];
    opts.colors = [T["viz-hs30"]];
    opts.fill = {
      type: "gradient",
      gradient: { shadeIntensity: 0, opacityFrom: 0.22, opacityTo: 0.02, stops: [0, 100] },
    };
    var fcScale = timeScale(series[0].x, series[series.length - 1].x, maxTicks(box));
    opts.xaxis.min = fcScale.min;
    opts.xaxis.max = fcScale.max;
    opts.xaxis.tickAmount = fcScale.tickAmount;
    opts.yaxis = {
      tickAmount: 3,
      labels: {
        style: { colors: T.muted, fontSize: "11.5px" },
        formatter: function (v) { return v.toFixed(2); },
      },
    };
    // The minimum is the number this chart exists to report, so it is the one
    // point that carries a direct label. It rides above its dot, and steps aside
    // when the dot sits against an edge so the label is never clipped.
    var span = series[series.length - 1].x - series[0].x;
    var place = span ? (lowest.x - series[0].x) / span : 0.5;
    opts.annotations = {
      points: [{
        x: lowest.x,
        y: lowest.y,
        marker: { size: 4, fillColor: T["viz-hs30"], strokeColor: T.surface, strokeWidth: 2 },
        label: {
          text: "mín " + fixed(lowest.y, 3),
          offsetY: -8,
          offsetX: place > 0.8 ? -34 : (place < 0.2 ? 34 : 0),
          borderColor: T.line,
          borderRadius: 6,
          style: {
            background: T.surface,
            color: T.ink,
            fontSize: "11.5px",
            fontWeight: 600,
            padding: { left: 6, right: 6, top: 3, bottom: 3 },
          },
        },
      }],
    };
    opts.tooltip.custom = sharedTooltip("VWC", 3);
    charts.forecast = new ApexCharts(box, opts);
    charts.forecast.render().then(function () { ready(box); });
  }

  mountReadings();
  mountForecast();
})();
