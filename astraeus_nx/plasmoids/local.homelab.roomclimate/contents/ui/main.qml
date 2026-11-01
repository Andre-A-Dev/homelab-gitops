import QtQuick
import org.kde.plasma.plasmoid
import org.kde.kirigami as Kirigami

// Reads the netatmo-exporter series from Prometheus. No Netatmo token here:
// the exporter on Mnemosyne stays the only client of the Netatmo API.
PlasmoidItem {
    id: root

    property var homes: []        // [{name, modules: [{key, home, module, values}]}]
    property var history: ({})    // key -> {co2: [[t, v]], temp: [[t, v]]}
    property string errorText: ""
    property real now: Date.now() / 1000

    readonly property int lowBattery: 40          // percent
    readonly property int staleAfter: 30 * 60     // seconds since the last Netatmo reading
    readonly property int historyHours: 24
    readonly property int historyStep: 1800       // seconds; a gap above two steps is drawn as missing data

    readonly property var selected: pickSelected(homes)
    readonly property bool selectedFallback: selected !== null && Plasmoid.configuration.module !== ""
        && selected.key !== Plasmoid.configuration.home + "/" + Plasmoid.configuration.module

    preferredRepresentation: compactRepresentation
    compactRepresentation: CompactView {}
    fullRepresentation: FullView {}

    toolTipMainText: selected ? selected.module : i18n("Room Climate")
    toolTipSubText: errorText !== "" ? errorText : (selected ? summary(selected) : i18n("No data yet"))

    function query(path, params, callback) {
        const base = Plasmoid.configuration.prometheusUrl.replace(/\/+$/, "");
        const qs = Object.keys(params).map(k => k + "=" + encodeURIComponent(params[k])).join("&");
        const xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== XMLHttpRequest.DONE) {
                return;
            }
            if (xhr.status !== 200) {
                callback(null, xhr.status === 0
                    ? i18n("Prometheus not reachable at %1", base)
                    : i18n("Prometheus answered HTTP %1", xhr.status));
                return;
            }
            try {
                const body = JSON.parse(xhr.responseText);
                if (body.status === "success") {
                    callback(body.data.result, "");
                } else {
                    callback(null, body.error || i18n("Prometheus query failed"));
                }
            } catch (e) {
                callback(null, i18n("Invalid response from Prometheus"));
            }
        };
        xhr.open("GET", base + path + "?" + qs);
        xhr.send();
    }

    function refresh() {
        query("/api/v1/query", {
            query: '{__name__=~"netatmo_sensor_(temperature_celsius|humidity_percent|co2_ppm|noise_db|pressure_mb|battery_percent|rain_amount_mm|wind_strength_kph|updated)"}'
        }, function (result, error) {
            errorText = error;
            now = Date.now() / 1000;
            if (result) {
                homes = buildHomes(result);
            }
        });

        const end = Math.floor(Date.now() / 1000);
        const collected = {};
        let pending = 2;
        [["co2", "netatmo_sensor_co2_ppm"], ["temp", "netatmo_sensor_temperature_celsius"]].forEach(function (pair) {
            query("/api/v1/query_range", {
                query: pair[1],
                start: end - historyHours * 3600,
                end: end,
                step: historyStep
            }, function (result, error) {
                (result || []).forEach(function (r) {
                    const key = r.metric.home + "/" + r.metric.module;
                    collected[key] = collected[key] || {};
                    collected[key][pair[0]] = r.values.map(v => [v[0], parseFloat(v[1])]);
                });
                pending -= 1;
                if (pending === 0) {
                    history = collected;
                }
            });
        });
    }

    // Base station first (the only module with a noise sensor), then indoor
    // modules, outdoor, and wind/rain gauges.
    function rank(values) {
        if (values.noise_db !== undefined) return 0;
        if (values.co2_ppm !== undefined) return 1;
        if (values.temperature_celsius !== undefined) return 2;
        return 3;
    }

    function buildHomes(result) {
        const modules = {};
        result.forEach(function (r) {
            const key = r.metric.home + "/" + r.metric.module;
            if (!modules[key]) {
                modules[key] = { key: key, home: r.metric.home, module: r.metric.module, values: {} };
            }
            modules[key].values[r.metric.__name__.replace("netatmo_sensor_", "")] = parseFloat(r.value[1]);
        });

        const byHome = {};
        Object.keys(modules).forEach(function (key) {
            const m = modules[key];
            (byHome[m.home] = byHome[m.home] || []).push(m);
        });

        // The home of the configured module comes first, so the popup opens on it.
        const configured = Plasmoid.configuration.home;
        return Object.keys(byHome).sort(function (a, b) {
            if (a === configured) return -1;
            if (b === configured) return 1;
            return a.localeCompare(b);
        }).map(function (name) {
            return {
                name: name,
                modules: byHome[name].sort((a, b) => rank(a.values) - rank(b.values) || a.module.localeCompare(b.module))
            };
        });
    }

    function pickSelected(homes) {
        const wanted = Plasmoid.configuration.home + "/" + Plasmoid.configuration.module;
        let fallback = null;
        for (const h of homes) {
            for (const m of h.modules) {
                if (m.key === wanted) return m;
                if (fallback === null && rank(m.values) === 0) fallback = m;
            }
        }
        return fallback !== null ? fallback : (homes.length > 0 ? homes[0].modules[0] : null);
    }

    function fmt(value, decimals) {
        return Number(value).toLocaleString(Qt.locale(), "f", decimals);
    }

    function co2Level(ppm) {
        if (ppm === undefined) return "";
        if (ppm >= Plasmoid.configuration.co2Critical) return "critical";
        if (ppm >= Plasmoid.configuration.co2Warning) return "warning";
        return "ok";
    }

    function levelColor(level) {
        if (level === "critical") return Kirigami.Theme.negativeTextColor;
        if (level === "warning") return Kirigami.Theme.neutralTextColor;
        return Kirigami.Theme.positiveTextColor;
    }

    function isStale(module) {
        return module !== null && module.values.updated !== undefined
            && now - module.values.updated > staleAfter;
    }

    function ageText(module) {
        if (module === null || module.values.updated === undefined) return "";
        const minutes = Math.max(0, Math.round((now - module.values.updated) / 60));
        if (minutes < 120) return i18np("updated %1 minute ago", "updated %1 minutes ago", minutes);
        return i18np("updated %1 hour ago", "updated %1 hours ago", Math.round(minutes / 60));
    }

    function summary(module) {
        const v = module.values;
        const parts = [];
        if (v.temperature_celsius !== undefined) parts.push(fmt(v.temperature_celsius, 1) + " °C");
        if (v.humidity_percent !== undefined) parts.push(i18n("%1 % humidity", fmt(v.humidity_percent, 0)));
        if (v.co2_ppm !== undefined) parts.push(i18n("CO₂ %1 ppm", fmt(v.co2_ppm, 0)));
        parts.push(ageText(module));
        return parts.join(" · ");
    }

    Timer {
        interval: Math.max(1, Plasmoid.configuration.refreshInterval) * 60 * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    // Keeps "updated N minutes ago" and the stale state current between refreshes.
    Timer {
        interval: 60 * 1000
        running: true
        repeat: true
        onTriggered: root.now = Date.now() / 1000
    }

    Connections {
        target: Plasmoid.configuration
        function onPrometheusUrlChanged() { root.refresh(); }
        function onHomeChanged() { root.refresh(); }
    }
}
