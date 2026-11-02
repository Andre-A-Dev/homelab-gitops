import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.kirigami as Kirigami

// One Netatmo module: values and sparkline; click expands a 24 h chart.
ColumnLayout {
    id: row

    property var module
    property bool expanded: false
    property string chartMetric: "co2"

    signal toggled()
    signal metricChosen(string metric)

    readonly property var v: module.values
    readonly property bool hasCo2: v.co2_ppm !== undefined
    readonly property bool hasTemp: v.temperature_celsius !== undefined
    readonly property var hist: root.history[module.key] || ({})
    readonly property bool hasHistory: (hist.co2 || hist.temp || []).length > 1
    readonly property string level: root.co2Level(v.co2_ppm)
    readonly property string shownMetric: hasCo2 && (chartMetric === "co2" || !hasTemp) ? "co2" : "temp"

    readonly property string mainValue: {
        if (hasTemp) return root.fmt(v.temperature_celsius, 1) + " °C";
        if (v.wind_strength_kph !== undefined) return i18n("%1 km/h", root.fmt(v.wind_strength_kph, 0));
        if (v.rain_amount_mm !== undefined) return i18n("%1 mm", root.fmt(v.rain_amount_mm, 1));
        return "–";
    }
    readonly property string details: {
        const parts = [];
        if (v.humidity_percent !== undefined) parts.push(i18n("%1 % humidity", root.fmt(v.humidity_percent, 0)));
        if (v.noise_db !== undefined) parts.push(i18n("%1 dB", root.fmt(v.noise_db, 0)));
        if (v.pressure_mb !== undefined) parts.push(i18n("%1 hPa", root.fmt(v.pressure_mb, 0)));
        return parts.join(" · ");
    }
    readonly property string warnings: {
        const parts = [];
        if (v.battery_percent !== undefined && v.battery_percent < root.lowBattery) {
            parts.push(i18n("Battery %1 %", root.fmt(v.battery_percent, 0)));
        }
        if (root.isStale(module)) {
            parts.push(root.ageText(module));
        }
        return parts.join(" · ");
    }

    spacing: 0

    PlasmaComponents.ItemDelegate {
        Layout.fillWidth: true
        highlighted: row.expanded
        enabled: row.hasHistory
        onClicked: row.toggled()

        contentItem: RowLayout {
            spacing: Kirigami.Units.largeSpacing

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    text: row.module.module
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }

                RowLayout {
                    visible: row.hasCo2
                    spacing: Kirigami.Units.smallSpacing

                    Rectangle {
                        implicitWidth: Kirigami.Units.smallSpacing * 2
                        implicitHeight: implicitWidth
                        radius: width / 2
                        color: root.levelColor(row.level)
                    }
                    PlasmaComponents.Label {
                        text: i18n("CO₂ %1 ppm", root.fmt(row.v.co2_ppm || 0, 0))
                        font: Kirigami.Theme.smallFont
                        color: row.level === "critical" ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                    }
                }

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    visible: text !== ""
                    text: row.details
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    elide: Text.ElideRight
                }

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    visible: text !== ""
                    text: row.warnings
                    font: Kirigami.Theme.smallFont
                    color: Kirigami.Theme.neutralTextColor
                    wrapMode: Text.Wrap
                }
            }

            TrendChart {
                Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                Layout.preferredHeight: Kirigami.Units.gridUnit * 1.5
                points: (row.hasCo2 ? row.hist.co2 : row.hist.temp) || []
                co2: row.hasCo2
                windowEnd: root.now
                gapAfter: root.historyStep * 2
                endColor: row.hasCo2 ? root.levelColor(row.level) : Kirigami.Theme.highlightColor
            }

            PlasmaComponents.Label {
                Layout.minimumWidth: Kirigami.Units.gridUnit * 4
                horizontalAlignment: Text.AlignRight
                text: row.mainValue
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.4
                font.weight: Font.DemiBold
            }
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        Layout.leftMargin: Kirigami.Units.largeSpacing
        Layout.rightMargin: Kirigami.Units.largeSpacing
        Layout.bottomMargin: Kirigami.Units.largeSpacing
        visible: row.expanded && row.hasHistory
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            visible: row.hasCo2 && row.hasTemp
            spacing: 0

            PlasmaComponents.ToolButton {
                text: i18n("CO₂ (ppm)")
                checkable: true
                checked: row.shownMetric === "co2"
                onClicked: row.metricChosen("co2")
            }
            PlasmaComponents.ToolButton {
                text: i18n("Temperature (°C)")
                checkable: true
                checked: row.shownMetric === "temp"
                onClicked: row.metricChosen("temp")
            }
        }

        TrendChart {
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 7
            axes: true
            points: (row.shownMetric === "co2" ? row.hist.co2 : row.hist.temp) || []
            co2: row.shownMetric === "co2"
            windowEnd: root.now
            gapAfter: root.historyStep * 2
            thresholds: row.shownMetric === "co2" ? [
                { value: Plasmoid.configuration.co2Warning, color: Kirigami.Theme.neutralTextColor },
                { value: Plasmoid.configuration.co2Critical, color: Kirigami.Theme.negativeTextColor }
            ] : []
            endColor: row.shownMetric === "co2" ? root.levelColor(row.level) : Kirigami.Theme.highlightColor
        }
    }
}
