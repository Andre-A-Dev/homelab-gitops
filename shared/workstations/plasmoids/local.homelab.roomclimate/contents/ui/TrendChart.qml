import QtQuick
import org.kde.kirigami as Kirigami

// Line chart over the last `hours`, used as sparkline (axes: false) and as
// the expanded chart in the popup (axes: true).
Canvas {
    id: chart

    property var points: []            // [[unix seconds, value]]
    property bool axes: false
    property bool co2: false           // CO2 scale starts at 0 with 1000 ppm ticks
    property var thresholds: []        // [{value, color}], drawn as dashed lines
    property color lineColor: Kirigami.Theme.highlightColor
    property color endColor: lineColor
    property real windowEnd: Date.now() / 1000
    property int hours: 24
    property real gapAfter: 3600       // seconds between two points that count as missing data

    readonly property color textColor: Kirigami.Theme.textColor

    onPointsChanged: requestPaint()
    onCo2Changed: requestPaint()
    onThresholdsChanged: requestPaint()
    onWindowEndChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    onTextColorChanged: requestPaint()
    onLineColorChanged: requestPaint()
    onEndColorChanged: requestPaint()

    function faded(c, alpha) {
        return Qt.rgba(c.r, c.g, c.b, alpha);
    }

    onPaint: {
        const ctx = getContext("2d");
        ctx.reset();
        const pts = points || [];
        if (pts.length < 2) {
            return;
        }

        const padL = axes ? Kirigami.Units.gridUnit * 2 : 1;
        const padR = axes ? Kirigami.Units.smallSpacing * 2 : 4;
        const padT = axes ? Kirigami.Units.smallSpacing : 3;
        const padB = axes ? Kirigami.Units.gridUnit : 3;
        const t1 = windowEnd;
        const t0 = t1 - hours * 3600;

        const values = pts.map(p => p[1]);
        let lo, hi, step;
        if (co2) {
            step = 1000;
            lo = 0;
            hi = Math.ceil(Math.max(...values, 1) / step) * step;
        } else {
            lo = Math.floor(Math.min(...values));
            hi = Math.ceil(Math.max(...values));
            step = hi - lo > 6 ? 5 : 1;
            lo = Math.floor(lo / step) * step;
            hi = Math.ceil(hi / step) * step;
            if (hi === lo) {
                hi += step;
            }
        }

        const X = t => padL + (t - t0) / (t1 - t0) * (width - padL - padR);
        const Y = v => padT + (1 - (v - lo) / (hi - lo)) * (height - padT - padB);
        const base = height - padB;
        const fontPx = Math.round(Kirigami.Units.gridUnit * 0.6);
        // Generic family: Context2D rejects some theme family names.
        ctx.font = fontPx + "px sans-serif";

        if (axes) {
            ctx.lineWidth = 1;
            ctx.strokeStyle = faded(textColor, 0.15);
            ctx.fillStyle = faded(textColor, 0.7);
            ctx.textAlign = "right";
            ctx.textBaseline = "middle";
            for (let v = lo; v <= hi; v += step) {
                const y = Math.round(Y(v)) + 0.5;
                ctx.beginPath();
                ctx.moveTo(padL, y);
                ctx.lineTo(width - padR, y);
                ctx.stroke();
                ctx.fillText(v.toLocaleString(Qt.locale(), "f", 0), padL - 4, y);
            }

            ctx.textAlign = "center";
            ctx.textBaseline = "bottom";
            for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) {
                const d = new Date(t * 1000);
                if (d.getHours() % 6 !== 0) {
                    continue;
                }
                const label = Qt.formatTime(d, "HH:mm");
                const half = ctx.measureText(label).width / 2;
                ctx.fillText(label, Math.min(Math.max(X(t), padL + half), width - half), height);
            }
        }

        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        (thresholds || []).forEach(function (th) {
            if (th.value > lo && th.value < hi) {
                const y = Math.round(Y(th.value)) + 0.5;
                ctx.strokeStyle = th.color;
                ctx.beginPath();
                ctx.moveTo(padL, y);
                ctx.lineTo(width - padR, y);
                ctx.stroke();
            }
        });
        ctx.setLineDash([]);

        const segments = [];
        let segment = [];
        pts.forEach(function (p, i) {
            if (i > 0 && p[0] - pts[i - 1][0] > gapAfter) {
                segments.push(segment);
                segment = [];
            }
            segment.push(p);
        });
        segments.push(segment);

        if (axes) {
            for (let i = 1; i < segments.length; i++) {
                const a = X(segments[i - 1][segments[i - 1].length - 1][0]);
                const b = X(segments[i][0][0]);
                ctx.fillStyle = ctx.createPattern(faded(textColor, 0.25), Qt.BDiagPattern);
                ctx.fillRect(a, padT, b - a, base - padT);
                const label = i18n("no data");
                if (b - a > ctx.measureText(label).width + 8) {
                    ctx.fillStyle = faded(textColor, 0.7);
                    ctx.textAlign = "center";
                    ctx.textBaseline = "top";
                    ctx.fillText(label, (a + b) / 2, padT + 2);
                }
            }
        }

        segments.forEach(function (s) {
            ctx.beginPath();
            ctx.moveTo(X(s[0][0]), base);
            s.forEach(p => ctx.lineTo(X(p[0]), Y(p[1])));
            ctx.lineTo(X(s[s.length - 1][0]), base);
            ctx.closePath();
            ctx.fillStyle = faded(lineColor, 0.15);
            ctx.fill();

            ctx.beginPath();
            s.forEach((p, i) => i === 0 ? ctx.moveTo(X(p[0]), Y(p[1])) : ctx.lineTo(X(p[0]), Y(p[1])));
            ctx.strokeStyle = lineColor;
            ctx.lineWidth = axes ? 1.6 : 1.3;
            ctx.lineJoin = "round";
            ctx.stroke();
        });

        const last = pts[pts.length - 1];
        ctx.beginPath();
        ctx.arc(X(last[0]), Y(last[1]), axes ? 3.5 : 2.5, 0, 2 * Math.PI);
        ctx.fillStyle = endColor;
        ctx.fill();
    }
}
