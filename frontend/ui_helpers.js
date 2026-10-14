(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PoeUI = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function escapeHTML(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c];
    });
  }
  function imageURL(value, origin) {
    try {
      var url = new URL(value, origin);
      return url.protocol === 'https:' || (url.protocol === 'http:' && url.origin === new URL(origin).origin) ? url.href : '';
    } catch (_) { return ''; }
  }
  function mergeTrend(existing, incoming, windowStart, bucketMillis) {
    var points = new Map();
    (existing || []).concat(incoming || []).forEach(function (point) {
      if (point && Number.isFinite(Date.parse(point.time))) points.set(String(point.key), point);
    });
    var cutoff = Date.parse(windowStart);
    return Array.from(points.values()).filter(function (point) {
      return Date.parse(point.time) + (bucketMillis || 0) >= cutoff;
    }).sort(function (a, b) { return Date.parse(a.time) - Date.parse(b.time) || String(a.key).localeCompare(String(b.key)); });
  }
  function eventKey(event) { return String(event.target_id) + '|' + String(event.item_id || event.id); }
  return {escapeHTML:escapeHTML, imageURL:imageURL, mergeTrend:mergeTrend, eventKey:eventKey};
});
