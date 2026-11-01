import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.kirigami as Kirigami

// Two lines like the digital clock: temperature on top, CO2 below,
// both from the module chosen in the settings.
MouseArea {
    id: compact

    readonly property var values: root.selected ? root.selected.values : ({})
    readonly property bool vertical: Plasmoid.formFactor === PlasmaCore.Types.Vertical
    // Font sizes follow the panel thickness, as the clock does.
    readonly property real basis: vertical ? Kirigami.Units.gridUnit * 2.5 : height
    readonly property string level: root.co2Level(values.co2_ppm)

    Layout.minimumWidth: vertical ? -1 : content.implicitWidth
    Layout.preferredWidth: vertical ? -1 : content.implicitWidth
    Layout.minimumHeight: vertical ? content.implicitHeight : -1

    hoverEnabled: true
    onClicked: root.expanded = !root.expanded

    RowLayout {
        id: content
        anchors.centerIn: parent
        spacing: Kirigami.Units.smallSpacing
        // Dimmed when the values are old or Prometheus is unreachable.
        opacity: root.errorText !== "" || root.isStale(root.selected) ? 0.5 : 1

        Kirigami.Icon {
            source: "temperature-normal-symbolic"
            Layout.preferredWidth: Math.round(compact.basis * 0.45)
            Layout.preferredHeight: Layout.preferredWidth
        }

        ColumnLayout {
            spacing: 0

            PlasmaComponents.Label {
                text: compact.values.temperature_celsius !== undefined
                    ? root.fmt(compact.values.temperature_celsius, 1) + "°"
                    : "–"
                font.pixelSize: Math.max(8, Math.round(compact.basis * 0.38))
            }

            PlasmaComponents.Label {
                visible: compact.values.co2_ppm !== undefined
                text: i18n("%1 ppm", root.fmt(compact.values.co2_ppm || 0, 0))
                font.pixelSize: Math.max(8, Math.round(compact.basis * 0.28))
                font.weight: compact.level === "ok" ? Font.Normal : Font.DemiBold
                color: compact.level === "ok" ? Kirigami.Theme.textColor : root.levelColor(compact.level)
                opacity: compact.level === "ok" ? 0.7 : 1
            }
        }
    }
}
