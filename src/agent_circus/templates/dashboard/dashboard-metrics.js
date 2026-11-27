/* agent-circus metric history: in-memory time series per sandbox.
   Plain ES2020, no framework, no DOM access (see the interface comment
   on the factory below). The browser page builds history from the
   normal polling responses; nothing is stored beyond the page
   lifetime. */
/* global globalThis */

"use strict";

(function () {
  // ---- limits ---------------------------------------------------------------

  // Retention window in seconds of data time (point timestamps), not
  // wall time, so a paused tab does not silently empty the store.
  var RETENTION_S = 600;
  // Hard cap of points per sandbox and series.
  var MAX_POINTS = 300;
  // A returned point is marked gap_before when the prior point is more
  // than this many seconds earlier (poll cadence is about 3 s).
  var GAP_S = 10;
  // Point cap for overview sparklines; detail charts use all points.
  var OVERVIEW_POINTS = 60;

  // ---- validation helpers ---------------------------------------------------

  /** True for a finite, non-negative number (no Booleans). */
  function isCount(n) {
    return typeof n === "number" && isFinite(n) && n >= 0;
  }

  /** Percent share of used against a total, or null. */
  function usedPercent(used, total) {
    if (!isCount(used) || !isCount(total) || total <= 0) return null;
    return (100 * used) / total;
  }

  /**
   * Split an across-cores CPU percent into capacity and core use.
   * cpu_percent is use across all cores (100 means one busy core), so
   * capacity_percent is cpu_percent / cpu_count and used_cores is
   * cpu_percent / 100. Returns null when either value cannot produce
   * a truthful capacity result (missing, zero, Boolean, or non-finite
   * count).
   */
  function cpuUsage(cpuPercent, cpuCount) {
    if (
      !isCount(cpuPercent) ||
      typeof cpuCount !== "number" ||
      !isFinite(cpuCount) ||
      cpuCount <= 0
    ) {
      return null;
    }
    return {
      used_cores: cpuPercent / 100,
      capacity_percent: cpuPercent / cpuCount,
    };
  }

  /**
   * Horizontal time range of a chart, in data seconds: the
   * RETENTION_S window that ends at the newest point, so sparse
   * history never stretches across the full chart width. An empty or
   * unusable series returns a finite placeholder range.
   */
  function chartTimeBounds(points) {
    var last = points.length > 0 ? points[points.length - 1] : null;
    var end = last !== null && isCount(last.t) ? last.t : 0;
    return { start: end - RETENTION_S, end: end };
  }

  /**
   * Build one history point for a series from a resources object.
   * Returns null when this series has no usable sample.
   */
  function pointFor(id, resources) {
    var t = resources.sampled_at;
    if (!isCount(t)) return null;
    if (id === "cpu") {
      var usage = cpuUsage(resources.cpu_percent, resources.cpu_count);
      return {
        t: t,
        percent: usage === null ? null : usage.capacity_percent,
        used_bytes: null,
        available_bytes: null,
        total_bytes: null,
      };
    }
    if (id === "memory") {
      var used = resources.memory_used_bytes;
      var available = resources.memory_available_bytes;
      var limit = resources.memory_limit_bytes;
      if (!isCount(used) || !isCount(limit)) return null;
      return {
        t: t,
        percent: usedPercent(used, limit),
        used_bytes: used,
        available_bytes: isCount(available) ? available : null,
        total_bytes: limit,
      };
    }
    if (id.indexOf("disk:") === 0) {
      var diskId = id.slice(5);
      var disks = Array.isArray(resources.disks) ? resources.disks : [];
      for (var i = 0; i < disks.length; i++) {
        var disk = disks[i];
        if (
          disk &&
          disk.id === diskId &&
          isCount(disk.used_bytes) &&
          isCount(disk.available_bytes)
        ) {
          return {
            t: t,
            percent: usedPercent(disk.used_bytes, disk.used_bytes + disk.available_bytes),
            used_bytes: disk.used_bytes,
            available_bytes: disk.available_bytes,
            total_bytes: isCount(disk.total_bytes) ? disk.total_bytes : null,
          };
        }
      }
      return null;
    }
    return null;
  }

  // ---- store ----------------------------------------------------------------

  /**
   * Create an independent history store.
   *
   * The store keeps at most MAX_POINTS per sandbox and series, drops
   * points older than RETENTION_S, and forgets everything when the
   * proxy restarts (proxy.started_at changes) or a sandbox disappears
   * from the state. Each series is appended only when the sample's
   * sampled_at differs from the stored one, so cached or repeated
   * responses never create duplicate points. Stalled samples are never
   * appended: the series keeps a gap until a running sample returns.
   *
   * Series identifiers: "cpu", "memory", "disk:root", "disk:docker".
   *
   * record(data) ingests one parsed /api/state payload.
   * series(name, id, limit) returns points oldest-first, each
   * {t, percent, used_bytes, available_bytes, total_bytes, gap_before};
   * percent is null without a valid denominator, and gap_before marks
   * a prior point more than GAP_S seconds earlier.
   */
  function createHistory() {
    var proxyStartedAt = null;
    /** name -> { lastSampledAt: number|null, series: Map(id -> points[]) } */
    var sandboxes = new Map();

    function drop(name) {
      sandboxes.delete(name);
    }

    /** Trim one series to the retention window and point cap. */
    function trim(points) {
      if (points.length === 0) return;
      var newest = points[points.length - 1].t;
      var cutoff = newest - RETENTION_S;
      while (points.length > 0 && points[0].t < cutoff) points.shift();
      while (points.length > MAX_POINTS) points.shift();
    }

    function recordSandbox(view) {
      var resources = view && view.resources;
      if (!resources || resources.sample_state !== "running") return;
      var sampledAt = resources.sampled_at;
      if (!isCount(sampledAt)) return;
      var store = sandboxes.get(view.name);
      if (store === undefined) {
        store = { lastSampledAt: null, series: new Map() };
        sandboxes.set(view.name, store);
      }
      if (store.lastSampledAt === sampledAt) return; // cached duplicate
      var ids = ["cpu", "memory", "disk:root", "disk:docker"];
      for (var i = 0; i < ids.length; i++) {
        var point = pointFor(ids[i], resources);
        if (point === null) continue;
        var points = store.series.get(ids[i]);
        if (points === undefined) {
          points = [];
          store.series.set(ids[i], points);
        }
        points.push(point);
        trim(points);
      }
      store.lastSampledAt = sampledAt;
    }

    return {
      record: function (data) {
        if (!data || !data.proxy) return;
        var startedAt = data.proxy.started_at;
        // A changed proxy start time means the history predates the
        // running proxy: drop it instead of mixing lifetimes.
        if (
          proxyStartedAt !== null &&
          startedAt !== null &&
          startedAt !== undefined &&
          startedAt !== proxyStartedAt
        ) {
          sandboxes.clear();
        }
        proxyStartedAt =
          startedAt === undefined || startedAt === null ? proxyStartedAt : startedAt;
        var views = Array.isArray(data.sandboxes) ? data.sandboxes : [];
        var seen = new Set();
        views.forEach(function (view) {
          if (view && typeof view.name === "string") seen.add(view.name);
        });
        Array.from(sandboxes.keys()).forEach(function (name) {
          if (!seen.has(name)) drop(name);
        });
        views.forEach(recordSandbox);
      },
      series: function (name, id, limit) {
        var store = sandboxes.get(name);
        if (store === undefined) return [];
        var points = store.series.get(id);
        if (points === undefined) return [];
        var cap = typeof limit === "number" && limit > 0 ? limit : points.length;
        var slice = points.slice(Math.max(points.length - cap, 0));
        return slice.map(function (point, index) {
          var prior = index > 0 ? slice[index - 1] : null;
          return {
            t: point.t,
            percent: point.percent,
            used_bytes: point.used_bytes,
            available_bytes: point.available_bytes,
            total_bytes: point.total_bytes,
            gap_before: prior !== null && point.t - prior.t > GAP_S,
          };
        });
      },
    };
  }

  globalThis.AgentCircusMetricHistory = {
    createHistory: createHistory,
    cpuUsage: cpuUsage,
    chartTimeBounds: chartTimeBounds,
    RETENTION_S: RETENTION_S,
    MAX_POINTS: MAX_POINTS,
    GAP_S: GAP_S,
    OVERVIEW_POINTS: OVERVIEW_POINTS,
  };
})();
