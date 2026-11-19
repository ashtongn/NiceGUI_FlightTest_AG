// Offline map for the mission planning tab: MapLibre GL JS drawing Protomaps
// vector tiles out of PMTiles files served by the planner itself. Everything
// (library, style, fonts, icons, tiles) is local; nothing is fetched from the
// internet at run time.
//
// Python drives it through window.missionMap (init, setPath, setMarkers,
// setGeofences, setOverlays, setPosition, setPositions, setAreas, enableDrop, fit, flyTo,
// setDrawMode) and hears back through NiceGUI's emitEvent('mission_map', {...})
// with type: ready, view, click, marker_click, drawn, draw_cancel, drop, error.
//
// Overlays are GeoJSON sources: the flight path (review only, neutral grey:
// it belongs to the plan, not to a radio), one marker per step location, the
// geofences, the imported overlay layers (lines, areas and points the
// operator only wants to see), the platform position (a dot, or an arrow
// turned to the heading when the feed carries one) and the shape being
// drawn. Markers and geofences come coloured per radio from the Python side
// (property color), with the radio's name in every label; a start area has a
// solid edge and a filled marker, a stop area a dashed edge and a hollow
// marker, and the selected radio's areas get a thicker edge.
//
// Files dropped anywhere on the map go to a hidden NiceGUI uploader
// (enableDrop names it), which posts them to the server over HTTP; the Python
// side sniffs the format and asks what to do with what is in them.
(function () {
  'use strict';
  const M = {};
  let map = null, cfg = null, ready = false;
  const queue = [];
  let draw = null;                         // {mode, target, pts: [[lon,lat],...]}
  let lastView = 0;
  const S = {path: 'mm-path', pathv: 'mm-path-v', markers: 'mm-markers',
             fences: 'mm-fences', pos: 'mm-pos', draw: 'mm-draw', drawv: 'mm-draw-v',
             ov: 'mm-ov', ovp: 'mm-ov-p'};
  let data = {path: [], markers: [], fences: [], pos: [], overlays: []};
  const PATH_COLOR = '#374151';            // same as map_element.PATH_COLOR
  let dropUploader = null;                 // NiceGUI element id of the hidden uploader
  let dropDepth = 0;

  function emit(d) {
    try { window.emitEvent('mission_map', d); } catch (e) { console.warn('mission_map emit', e); }
  }

  function libsReady() {
    return !!(window.maplibregl && window.pmtiles && window.protomapsBasemaps);
  }

  function whenLibs(cb, tries) {
    tries = tries || 0;
    if (libsReady()) { cb(); return; }
    if (tries > 200) { emit({type: 'error', message: 'map libraries did not load'}); return; }
    setTimeout(() => whenLibs(cb, tries + 1), 50);
  }

  // a circle on the ground as a polygon ring, [lon, lat] vertices
  function circleRing(lat, lon, rM, n) {
    n = n || 64;
    const out = [];
    const dLat = rM / 111320.0;
    const dLon = rM / (111320.0 * Math.cos(lat * Math.PI / 180) || 1e-9);
    for (let i = 0; i <= n; i++) {
      const a = 2 * Math.PI * i / n;
      out.push([lon + dLon * Math.sin(a), lat + dLat * Math.cos(a)]);
    }
    return out;
  }

  function fc(features) { return {type: 'FeatureCollection', features: features}; }

  function buildStyle() {
    const B = window.protomapsBasemaps;
    const origin = window.location.origin;
    const flavorName = cfg.flavor || 'light';
    const flavor = B.namedFlavor(flavorName);
    const style = {
      version: 8,
      glyphs: origin + cfg.assetsUrl + '/fonts/{fontstack}/{range}.pbf',
      sprite: origin + cfg.assetsUrl + '/sprites/v4/' + flavorName,
      sources: {},
      layers: [],
    };
    let haveBg = false;
    const addSrc = (name, url, maxzoom, tileType) => {
      if (tileType && tileType !== 'vector') {
        // a dropped raster tile set (png, jpeg, webp): one image layer
        style.sources[name] = {type: 'raster', url: 'pmtiles://' + origin + url,
                               tileSize: 256, attribution: ''};
        if (maxzoom) style.sources[name].maxzoom = maxzoom;
        style.layers.push({id: name + '-raster', type: 'raster', source: name});
        return;
      }
      style.sources[name] = {type: 'vector', url: 'pmtiles://' + origin + url,
                             attribution: 'Protomaps, OpenStreetMap'};
      if (maxzoom) style.sources[name].maxzoom = maxzoom;
      for (const l of B.layers(name, flavor, {lang: 'en'})) {
        if (l.type === 'background') {
          if (haveBg) continue;
          haveBg = true;
        }
        l.id = name + '-' + l.id;
        style.layers.push(l);
      }
    };
    if (cfg.baseUrl) addSrc('base', cfg.baseUrl, cfg.baseMaxzoom || 7);
    for (const a of (cfg.areas || [])) addSrc('area-' + a.name, a.url, a.maxzoom || 14, a.tileType);
    if (!haveBg) {
      style.layers.unshift({id: 'mm-bg', type: 'background',
                            paint: {'background-color': '#e5e7eb'}});
    }
    return style;
  }

  function addOverlays() {
    const add = (id, geo) => {
      if (map.getSource(id)) map.getSource(id).setData(geo);
      else map.addSource(id, {type: 'geojson', data: geo});
    };
    add(S.path, fc([])); add(S.pathv, fc([])); add(S.fences, fc([]));
    add(S.markers, fc([])); add(S.pos, fc([])); add(S.draw, fc([])); add(S.drawv, fc([]));
    add(S.ov, fc([])); add(S.ovp, fc([]));
    const layers = [
      {id: 'mm-ov-fill', type: 'fill', source: S.ov, filter: ['==', '$type', 'Polygon'],
       paint: {'fill-color': ['get', 'color'], 'fill-opacity': 0.12}},
      {id: 'mm-ov-line', type: 'line', source: S.ov,
       paint: {'line-color': ['get', 'color'], 'line-width': 2, 'line-dasharray': [3, 2]}},
      {id: 'mm-ov-label', type: 'symbol', source: S.ov,
       layout: {'text-field': ['get', 'label'], 'text-font': ['Noto Sans Regular'],
                'text-size': 10, 'symbol-placement': 'point'},
       paint: {'text-color': ['get', 'color'], 'text-halo-color': '#ffffff',
               'text-halo-width': 1.2}},
      {id: 'mm-ov-pts', type: 'circle', source: S.ovp,
       paint: {'circle-radius': 5, 'circle-color': ['get', 'color'],
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5}},
      {id: 'mm-ov-pts-t', type: 'symbol', source: S.ovp,
       layout: {'text-field': ['get', 'label'], 'text-font': ['Noto Sans Regular'],
                'text-size': 10, 'text-anchor': 'left', 'text-offset': [0.7, 0]},
       paint: {'text-color': ['get', 'color'], 'text-halo-color': '#ffffff',
               'text-halo-width': 1.2}},
      {id: 'mm-fences-fill', type: 'fill', source: S.fences,
       paint: {'fill-color': ['get', 'color'],
               'fill-opacity': ['case', ['==', ['get', 'role'], 'stop'], 0.07, 0.18]}},
      {id: 'mm-fences-line', type: 'line', source: S.fences,
       filter: ['!=', ['get', 'role'], 'stop'],
       paint: {'line-color': ['get', 'color'],
               'line-width': ['case', ['==', ['get', 'selected'], true], 3.5, 2]}},
      {id: 'mm-fences-line-stop', type: 'line', source: S.fences,
       filter: ['==', ['get', 'role'], 'stop'],
       paint: {'line-color': ['get', 'color'],
               'line-width': ['case', ['==', ['get', 'selected'], true], 3.5, 2],
               'line-dasharray': [3, 2]}},
      {id: 'mm-fences-label', type: 'symbol', source: S.fences,
       layout: {'text-field': ['get', 'label'], 'text-font': ['Noto Sans Regular'],
                'text-size': 11, 'text-anchor': 'top', 'text-offset': [0, 0.4]},
       paint: {'text-color': ['get', 'color'], 'text-halo-color': '#ffffff',
               'text-halo-width': 1.2}},
      {id: 'mm-path-line', type: 'line', source: S.path,
       paint: {'line-color': PATH_COLOR, 'line-width': 3, 'line-opacity': 0.9}},
      {id: 'mm-path-v', type: 'circle', source: S.pathv,
       paint: {'circle-radius': 3, 'circle-color': PATH_COLOR,
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1}},
      {id: 'mm-draw-line', type: 'line', source: S.draw,
       paint: {'line-color': '#f59e0b', 'line-width': 2, 'line-dasharray': [2, 1]}},
      {id: 'mm-draw-fill', type: 'fill', source: S.draw,
       paint: {'fill-color': '#f59e0b', 'fill-opacity': 0.1}},
      {id: 'mm-draw-v', type: 'circle', source: S.drawv,
       paint: {'circle-radius': 4, 'circle-color': '#f59e0b',
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1}},
      {id: 'mm-markers-c', type: 'circle', source: S.markers,
       paint: {'circle-radius': ['case', ['==', ['get', 'selected'], true], 8, 7],
               'circle-color': ['case', ['==', ['get', 'fill'], false], '#ffffff', ['get', 'color']],
               'circle-stroke-color': ['case', ['==', ['get', 'fill'], false], ['get', 'color'], '#ffffff'],
               'circle-stroke-width': ['case', ['==', ['get', 'fill'], false], 2.5, 2]}},
      {id: 'mm-markers-t', type: 'symbol', source: S.markers,
       layout: {'text-field': ['get', 'label'], 'text-font': ['Noto Sans Regular'],
                'text-size': 11, 'text-anchor': 'left', 'text-offset': [0.8, 0],
                'text-allow-overlap': true},
       paint: {'text-color': '#111827', 'text-halo-color': '#ffffff',
               'text-halo-width': 1.5}},
      {id: 'mm-pos-halo', type: 'circle', source: S.pos,
       paint: {'circle-radius': 14, 'circle-color': ['get', 'color'],
               'circle-opacity': 0.25}},
      // a dot when no heading is known, an arrow turned to the heading when it is
      {id: 'mm-pos', type: 'circle', source: S.pos, filter: ['!', ['has', 'heading']],
       paint: {'circle-radius': 7, 'circle-color': ['get', 'color'],
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2}},
      {id: 'mm-pos-arrow', type: 'symbol', source: S.pos, filter: ['has', 'heading'],
       layout: {'icon-image': ['get', 'arrow'], 'icon-size': 1,
                'icon-rotate': ['get', 'heading'], 'icon-rotation-alignment': 'map',
                'icon-allow-overlap': true, 'icon-ignore-placement': true},
       paint: {'icon-opacity': 1}},
      {id: 'mm-pos-t', type: 'symbol', source: S.pos,
       layout: {'text-field': ['get', 'label'], 'text-font': ['Noto Sans Regular'],
                'text-size': 11, 'text-anchor': 'top', 'text-offset': [0, 1.0],
                'text-allow-overlap': true},
       paint: {'text-color': '#111827', 'text-halo-color': '#ffffff',
               'text-halo-width': 1.5}},
    ];
    addArrowImages();
    for (const l of layers) if (!map.getLayer(l.id)) map.addLayer(l);
    applyAll();
  }

  // the platform arrow, drawn once per colour on a canvas: a triangle pointing
  // up (north) with a white edge, turned by icon-rotate to the heading
  function arrowImage(color) {
    const size = 36, c = document.createElement('canvas');
    c.width = c.height = size;
    const g = c.getContext('2d');
    g.clearRect(0, 0, size, size);
    g.beginPath();
    g.moveTo(size / 2, 3);                      // nose
    g.lineTo(size - 6, size - 6);               // right tail
    g.lineTo(size / 2, size - 13);              // notch
    g.lineTo(6, size - 6);                      // left tail
    g.closePath();
    g.fillStyle = color;
    g.fill();
    g.lineWidth = 2.5;
    g.strokeStyle = '#ffffff';
    g.lineJoin = 'round';
    g.stroke();
    return g.getImageData(0, 0, size, size);
  }

  const ARROWS = {'mm-arrow-live': '#7c3aed', 'mm-arrow-stale': '#6b7280'};

  function addArrowImages() {
    for (const name of Object.keys(ARROWS)) {
      if (map.hasImage(name)) continue;
      try { map.addImage(name, arrowImage(ARROWS[name]), {pixelRatio: 2}); }
      catch (e) { console.warn('arrow image', e); }
    }
  }

  function applyAll() {
    M.setPath(data.path); M.setMarkers(data.markers);
    M.setGeofences(data.fences); M.setPositions(data.pos);
    M.setOverlays(data.overlays);
  }

  function withMap(fn) {
    if (ready) fn(); else queue.push(fn);
  }

  // public API

  // The map div may not be in the document yet: in the main app the Mission
  // tab is one panel of the engineering view, and the browser only builds a
  // tab panel when the operator opens it, while the Python side sends init
  // right after the page loads. Wait for the div, however long that takes
  // (a cheap check twice a second), then build the map in it.
  function whenContainer(containerId, cb) {
    if (document.getElementById(containerId)) { cb(); return; }
    setTimeout(() => whenContainer(containerId, cb), 500);
  }

  M.init = function (containerId, c) {
    cfg = c || {};
    whenContainer(containerId, () => whenLibs(() => {
      try {
        const protocol = new window.pmtiles.Protocol();
        window.maplibregl.addProtocol('pmtiles', protocol.tile);
      } catch (e) { /* already registered */ }
      map = new window.maplibregl.Map({
        container: containerId,
        style: buildStyle(),
        center: cfg.center || [-111.5, 34.2],
        zoom: cfg.zoom == null ? 5.5 : cfg.zoom,
        attributionControl: false,
      });
      map.addControl(new window.maplibregl.NavigationControl({showCompass: false}), 'top-left');
      map.addControl(new window.maplibregl.ScaleControl({unit: 'metric'}), 'bottom-left');
      map.addControl(new window.maplibregl.AttributionControl({compact: true}));
      map.on('load', () => {
        addOverlays();
        ready = true;
        while (queue.length) { try { queue.shift()(); } catch (e) { console.warn(e); } }
        emit({type: 'ready'});
      });
      map.on('style.load', () => { if (ready) addOverlays(); });
      map.on('error', (e) => {
        const msg = (e && e.error && e.error.message) || 'map error';
        if (/pbf|tile/i.test(msg)) return;      // a missing tile is normal past the data's zoom
        emit({type: 'error', message: msg});
      });
      map.on('moveend', () => {
        const now = Date.now();
        if (now - lastView < 250) return;
        lastView = now;
        const c = map.getCenter();
        emit({type: 'view', lat: c.lat, lon: c.lng, zoom: map.getZoom()});
      });
      map.on('click', onClick);
      map.on('dblclick', onDblClick);
      map.on('mousemove', onMove);
      // dragging a step marker moves its area (Python hears marker_moved)
      map.on('mousedown', 'mm-markers-c', onMarkerDown);
      map.on('mouseenter', 'mm-markers-c', () => { if (!draw && !dragging) map.getCanvas().style.cursor = 'move'; });
      map.on('mouseleave', 'mm-markers-c', () => { if (!draw && !dragging) map.getCanvas().style.cursor = ''; });
      map.getCanvas().addEventListener('keydown', (ev) => { if (ev.key === 'Escape') M.setDrawMode(null); });
      document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && draw) M.setDrawMode(null); });
      installDrop(document.getElementById(containerId));
    }));
  };

  // file drop

  function toast(message, type) {
    try {
      if (window.Quasar && window.Quasar.Notify) {
        window.Quasar.Notify.create({message: message, type: type || 'info', position: 'top',
                                     timeout: 2500});
      }
    } catch (e) { /* no toast */ }
  }

  function hasFiles(ev) {
    const t = ev.dataTransfer;
    if (!t || !t.types) return false;
    for (const ty of t.types) if (ty === 'Files') return true;
    return false;
  }

  function installDrop(el) {
    if (!el || el.dataset.mmDrop) return;
    el.dataset.mmDrop = '1';
    el.addEventListener('dragenter', (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      dropDepth += 1;
      el.classList.add('mm-drop-hot');
    });
    el.addEventListener('dragover', (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'copy';
    });
    el.addEventListener('dragleave', (ev) => {
      if (!hasFiles(ev)) return;
      dropDepth = Math.max(0, dropDepth - 1);
      if (dropDepth === 0) el.classList.remove('mm-drop-hot');
    });
    el.addEventListener('drop', (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      dropDepth = 0;
      el.classList.remove('mm-drop-hot');
      const files = Array.from(ev.dataTransfer.files || []);
      if (!files.length) return;
      M.takeFiles(files);
    });
  }

  M.enableDrop = function (uploaderId, maxFiles) {
    dropUploader = uploaderId;
    cfg = cfg || {};
    cfg.maxDropFiles = maxFiles || 10;
  };

  // hand dropped (or otherwise obtained) File objects to the hidden uploader,
  // which posts them to the server in one request
  M.takeFiles = function (files) {
    const names = files.map((f) => f.name);
    const maxFiles = (cfg && cfg.maxDropFiles) || 10;
    if (files.length > maxFiles) {
      toast('drop at most ' + maxFiles + ' files at a time', 'warning');
      return false;
    }
    if (dropUploader == null || typeof window.getElement !== 'function') {
      toast('the map is not ready to take files yet', 'warning');
      return false;
    }
    const comp = window.getElement(dropUploader);
    const q = comp && comp.$refs && comp.$refs.qRef ? comp.$refs.qRef : comp;
    if (!q || typeof q.addFiles !== 'function') {
      toast('the map is not ready to take files yet', 'warning');
      return false;
    }
    const total = files.reduce((n, f) => n + (f.size || 0), 0);
    const size = total >= 1024 * 1024 ? (total / (1024 * 1024)).toFixed(1) + ' MB'
                                      : Math.max(1, Math.round(total / 1024)) + ' KB';
    toast('reading ' + (names.length === 1 ? names[0] : names.length + ' files') +
          ' (' + size + ')', 'info');
    emit({type: 'drop', names: names, bytes: total});
    try { q.reset(); } catch (e) { /* nothing queued */ }
    q.addFiles(files);
    return true;
  };

  M.setAreas = function (areas) {
    withMap(() => {
      cfg.areas = areas || [];
      map.setStyle(buildStyle());          // style.load re-adds the overlays
    });
  };

  M.setPath = function (coords) {
    data.path = coords || [];
    withMap(() => {
      const line = data.path.length > 1
        ? [{type: 'Feature', geometry: {type: 'LineString', coordinates: data.path}, properties: {}}]
        : [];
      map.getSource(S.path).setData(fc(line));
      map.getSource(S.pathv).setData(fc(data.path.map((p, i) => (
        {type: 'Feature', geometry: {type: 'Point', coordinates: p}, properties: {i: i}}))));
    });
  };

  M.setMarkers = function (list) {
    data.markers = list || [];
    withMap(() => { map.getSource(S.markers).setData(markerGeo()); });
  };

  M.setGeofences = function (list) {
    data.fences = list || [];
    withMap(() => {
      const feats = [];
      for (const g of data.fences) {
        let ring = null;
        if (g.kind === 'point') ring = circleRing(g.lat, g.lon, g.radius_m || 0);
        else if (g.kind === 'polygon' && g.polygon && g.polygon.length >= 3) {
          ring = g.polygon.slice();
          const a = ring[0], b = ring[ring.length - 1];
          if (a[0] !== b[0] || a[1] !== b[1]) ring.push(a);
        }
        if (!ring) continue;
        feats.push({type: 'Feature', geometry: {type: 'Polygon', coordinates: [ring]},
                    properties: {id: g.id, label: g.label || '', role: g.role || 'start',
                                 selected: g.selected === true,
                                 color: g.color || (g.role === 'stop' ? '#dc2626' : '#16a34a')}});
      }
      map.getSource(S.fences).setData(fc(feats));
    });
  };

  // overlay layers: [{id, name, color, lines: [{name, coords}], polygons: [...],
  // points: [{name, lat, lon, radius_m}]}]; a point with a radius is drawn as
  // a circle as well as a dot
  M.setOverlays = function (list) {
    data.overlays = list || [];
    withMap(() => {
      const shapes = [], pts = [];
      for (const ov of data.overlays) {
        const color = ov.color || '#0d9488';
        for (const ln of (ov.lines || [])) {
          if (!ln.coords || ln.coords.length < 2) continue;
          shapes.push({type: 'Feature', geometry: {type: 'LineString', coordinates: ln.coords},
                       properties: {label: ln.name || '', color: color, overlay: ov.id}});
        }
        for (const pg of (ov.polygons || [])) {
          if (!pg.coords || pg.coords.length < 3) continue;
          const ring = pg.coords.slice();
          const a = ring[0], b = ring[ring.length - 1];
          if (a[0] !== b[0] || a[1] !== b[1]) ring.push(a);
          shapes.push({type: 'Feature', geometry: {type: 'Polygon', coordinates: [ring]},
                       properties: {label: pg.name || '', color: color, overlay: ov.id}});
        }
        for (const p of (ov.points || [])) {
          if (p.lat == null || p.lon == null) continue;
          pts.push({type: 'Feature', geometry: {type: 'Point', coordinates: [p.lon, p.lat]},
                    properties: {label: p.name || '', color: color, overlay: ov.id}});
          if (p.radius_m > 0) {
            shapes.push({type: 'Feature',
                         geometry: {type: 'Polygon', coordinates: [circleRing(p.lat, p.lon, p.radius_m)]},
                         properties: {label: '', color: color, overlay: ov.id}});
          }
        }
      }
      map.getSource(S.ov).setData(fc(shapes));
      map.getSource(S.ovp).setData(fc(pts));
    });
  };

  // one marker per aircraft: a list of {lat, lon, label, valid, color, heading}
  M.setPositions = function (list) {
    data.pos = Array.isArray(list) ? list : (list ? [list] : []);
    withMap(() => {
      const feats = [];
      for (const p of data.pos) {
        if (!p || p.lat == null) continue;
        const stale = p.valid === false;
        const props = {label: p.label || 'platform',
                       color: stale ? '#6b7280' : (p.color || '#7c3aed'),
                       arrow: stale ? 'mm-arrow-stale' : 'mm-arrow-live'};
        // heading in degrees true turns the marker into an arrow (mm-pos-arrow)
        if (p.heading != null && isFinite(p.heading)) props.heading = Number(p.heading);
        feats.push({type: 'Feature',
                    geometry: {type: 'Point', coordinates: [p.lon, p.lat]},
                    properties: props});
      }
      map.getSource(S.pos).setData(fc(feats));
    });
  };

  // the one platform marker: a marker or null
  M.setPosition = function (p) { M.setPositions(p ? [p] : []); };

  function boundsOf(points) {
    if (!points.length) return null;
    const b = new window.maplibregl.LngLatBounds(points[0], points[0]);
    for (const p of points) b.extend(p);
    return b;
  }

  M.fit = function (what) {
    withMap(() => {
      let pts = [];
      if (what === 'path' || what === 'all') pts = pts.concat(data.path);
      if (what === 'markers' || what === 'all') pts = pts.concat(data.markers.map((m) => [m.lon, m.lat]));
      if (what === 'fences' || what === 'all') {
        for (const g of data.fences) {
          if (g.kind === 'point') pts = pts.concat(circleRing(g.lat, g.lon, g.radius_m || 0, 8));
          else if (g.polygon) pts = pts.concat(g.polygon);
        }
      }
      if (what === 'overlays' || what === 'all') {
        for (const ov of data.overlays) {
          for (const ln of (ov.lines || []).concat(ov.polygons || [])) pts = pts.concat(ln.coords || []);
          for (const p of (ov.points || [])) pts.push([p.lon, p.lat]);
        }
      }
      if (what === 'position' && data.pos) pts.push([data.pos.lon, data.pos.lat]);
      const b = boundsOf(pts);
      if (!b) return;
      map.fitBounds(b, {padding: 48, maxZoom: 13, duration: 500});
    });
  };

  M.fitBbox = function (w, s, e, n) {
    withMap(() => {
      map.fitBounds(new window.maplibregl.LngLatBounds([w, s], [e, n]),
                    {padding: 48, maxZoom: 13, duration: 500});
    });
  };

  M.flyTo = function (lat, lon, zoom) {
    withMap(() => { map.flyTo({center: [lon, lat], zoom: zoom == null ? map.getZoom() : zoom, duration: 600}); });
  };

  // drawing

  function drawGeo() {
    if (!draw) return [fc([]), fc([])];
    const pts = draw.pts;
    const feats = [];
    if (draw.mode === 'box' && pts.length === 2) {
      const [a, b] = pts;
      const ring = [[a[0], a[1]], [b[0], a[1]], [b[0], b[1]], [a[0], b[1]], [a[0], a[1]]];
      feats.push({type: 'Feature', geometry: {type: 'Polygon', coordinates: [ring]}, properties: {}});
    } else if (draw.mode === 'polygon' && pts.length >= 3) {
      feats.push({type: 'Feature', geometry: {type: 'Polygon', coordinates: [pts.concat([pts[0]])]}, properties: {}});
    } else if (pts.length > 1) {
      feats.push({type: 'Feature', geometry: {type: 'LineString', coordinates: pts}, properties: {}});
    }
    const v = pts.map((p) => ({type: 'Feature', geometry: {type: 'Point', coordinates: p}, properties: {}}));
    return [fc(feats), fc(v)];
  }

  function renderDraw() {
    if (!ready) return;
    const [a, b] = drawGeo();
    map.getSource(S.draw).setData(a);
    map.getSource(S.drawv).setData(b);
  }

  M.setDrawMode = function (mode, target) {
    withMap(() => {
      if (draw && !mode) emit({type: 'draw_cancel', mode: draw.mode, target: draw.target});
      draw = mode ? {mode: mode, target: target || '', pts: [], hover: null} : null;
      map.getCanvas().style.cursor = mode ? 'crosshair' : '';
      if (mode) map.doubleClickZoom.disable(); else map.doubleClickZoom.enable();
      renderDraw();
    });
  };

  function finishDraw() {
    if (!draw) return;
    const d = draw;
    draw = null;
    map.getCanvas().style.cursor = '';
    map.doubleClickZoom.enable();
    renderDraw();
    const pts = d.pts;
    if (d.mode === 'path' && pts.length >= 2) {
      emit({type: 'drawn', mode: 'path', target: d.target, coords: pts});
    } else if (d.mode === 'polygon' && pts.length >= 3) {
      emit({type: 'drawn', mode: 'polygon', target: d.target, polygon: pts});
    } else if (d.mode === 'box' && pts.length === 2) {
      const w = Math.min(pts[0][0], pts[1][0]), e = Math.max(pts[0][0], pts[1][0]);
      const s = Math.min(pts[0][1], pts[1][1]), n = Math.max(pts[0][1], pts[1][1]);
      emit({type: 'drawn', mode: 'box', target: d.target, bbox: [w, s, e, n]});
    } else if (d.mode === 'point' && pts.length === 1) {
      emit({type: 'drawn', mode: 'point', target: d.target, lat: pts[0][1], lon: pts[0][0]});
    } else {
      emit({type: 'draw_cancel', mode: d.mode, target: d.target});
    }
  }

  // marker drag

  let dragging = null;                     // {id, moved}

  function markerGeo() {
    return fc(data.markers.map((m) => ({
      type: 'Feature', geometry: {type: 'Point', coordinates: [m.lon, m.lat]},
      properties: {id: m.id, label: m.label || '', color: m.color || '#2563eb',
                   fill: m.fill !== false, role: m.role || 'start',
                   selected: m.selected === true}})));
  }

  function onMarkerDown(ev) {
    if (draw || !ev.features || !ev.features.length) return;
    ev.preventDefault();
    const id = ev.features[0].properties.id;
    dragging = {id: id, moved: false};
    map.dragPan.disable();
    map.getCanvas().style.cursor = 'grabbing';
    map.on('mousemove', onMarkerMove);
    map.once('mouseup', onMarkerUp);
  }

  function onMarkerMove(ev) {
    if (!dragging) return;
    const m = data.markers.find((x) => x.id === dragging.id);
    if (!m) return;
    m.lon = ev.lngLat.lng;
    m.lat = ev.lngLat.lat;
    dragging.moved = true;
    map.getSource(S.markers).setData(markerGeo());
  }

  function onMarkerUp(ev) {
    map.off('mousemove', onMarkerMove);
    map.dragPan.enable();
    map.getCanvas().style.cursor = '';
    const d = dragging;
    dragging = null;
    if (!d) return;
    if (d.moved) {
      emit({type: 'marker_moved', id: d.id, lat: ev.lngLat.lat, lon: ev.lngLat.lng});
      suppressClick = Date.now();
    }
  }

  let suppressClick = 0;

  function onClick(ev) {
    const p = [ev.lngLat.lng, ev.lngLat.lat];
    if (Date.now() - suppressClick < 300) return;     // the mouseup of a drag
    if (draw) {
      draw.pts.push(p);
      renderDraw();
      if (draw.mode === 'point' || (draw.mode === 'box' && draw.pts.length === 2)) finishDraw();
      return;
    }
    const hits = map.queryRenderedFeatures(ev.point, {layers: ['mm-markers-c']});
    if (hits && hits.length) {
      emit({type: 'marker_click', id: hits[0].properties.id, lat: p[1], lon: p[0]});
      return;
    }
    emit({type: 'click', lat: p[1], lon: p[0]});
  }

  function onDblClick(ev) {
    if (!draw) return;
    ev.preventDefault();
    // the double click already added its point on the first click; drop the
    // duplicate the second click added
    if (draw.pts.length >= 2) {
      const a = draw.pts[draw.pts.length - 1], b = draw.pts[draw.pts.length - 2];
      if (a[0] === b[0] && a[1] === b[1]) draw.pts.pop();
    }
    finishDraw();
  }

  function onMove(ev) {
    if (!draw || !ready) return;
    // rubber band: show the shape with the cursor as the next vertex
    const pts = draw.pts.concat([[ev.lngLat.lng, ev.lngLat.lat]]);
    const saved = draw.pts;
    draw.pts = pts;
    const [a, b] = drawGeo();
    draw.pts = saved;
    map.getSource(S.draw).setData(a);
    map.getSource(S.drawv).setData(b);
  }

  M.project = function (lat, lon) {
    // pixel position of a place inside the map canvas (for tests and tools)
    if (!map) return null;
    const p = map.project([lon, lat]);
    return [p.x, p.y];
  };

  M.state = function () {
    return {ready: ready, draw: draw ? {mode: draw.mode, n: draw.pts.length} : null,
            center: map ? map.getCenter() : null, zoom: map ? map.getZoom() : null,
            layers: map && map.getStyle() ? map.getStyle().layers.length : 0,
            data: {path: data.path.length, markers: data.markers.length,
                   fences: data.fences.length, pos: !!data.pos,
                   overlays: data.overlays.length},
            drop: dropUploader != null};
  };

  M.resize = function () { if (map) map.resize(); };
  window.missionMap = M;
})();
