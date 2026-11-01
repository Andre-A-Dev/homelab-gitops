import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

PlasmaExtras.Representation {
    id: full

    property int homeIndex: 0
    property string expandedKey: root.selected ? root.selected.key : ""
    property string chartMetric: "co2"
    readonly property var home: root.homes.length > 0 ? root.homes[Math.min(homeIndex, root.homes.length - 1)] : null

    // Fixed implicit size: the Page default derives it from the ScrollView,
    // whose contentWidth depends on the width again (binding loop).
    implicitWidth: Kirigami.Units.gridUnit * 24
    implicitHeight: Kirigami.Units.gridUnit * 30
    Layout.preferredWidth: implicitWidth
    Layout.preferredHeight: implicitHeight
    Layout.minimumWidth: Kirigami.Units.gridUnit * 18
    Layout.minimumHeight: Kirigami.Units.gridUnit * 16

    collapseMarginsHint: true

    header: PlasmaExtras.PlasmoidHeading {
        contentItem: ColumnLayout {
            spacing: 0

            RowLayout {
                Kirigami.Heading {
                    Layout.fillWidth: true
                    level: 1
                    text: i18n("Room Climate")
                    elide: Text.ElideRight
                }
                PlasmaComponents.ToolButton {
                    visible: Plasmoid.configuration.grafanaUrl !== ""
                    icon.name: "office-chart-line"
                    text: i18n("Open in Grafana")
                    display: QQC2.AbstractButton.IconOnly
                    onClicked: Qt.openUrlExternally(Plasmoid.configuration.grafanaUrl)
                    QQC2.ToolTip.text: text
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
                PlasmaComponents.ToolButton {
                    icon.name: "view-refresh"
                    text: i18n("Refresh")
                    display: QQC2.AbstractButton.IconOnly
                    onClicked: root.refresh()
                    QQC2.ToolTip.text: text
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
                PlasmaComponents.ToolButton {
                    icon.name: "configure"
                    text: i18n("Configure…")
                    display: QQC2.AbstractButton.IconOnly
                    onClicked: Plasmoid.internalAction("configure").trigger()
                    QQC2.ToolTip.text: text
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
            }

            PlasmaComponents.TabBar {
                Layout.fillWidth: true
                visible: root.homes.length > 1
                currentIndex: full.homeIndex
                onCurrentIndexChanged: full.homeIndex = currentIndex

                Repeater {
                    model: root.homes
                    PlasmaComponents.TabButton {
                        text: modelData.name
                    }
                }
            }
        }
    }

    contentItem: PlasmaComponents.ScrollView {
        contentWidth: availableWidth - contentItem.leftMargin - contentItem.rightMargin

        contentItem: ListView {
            id: list
            model: full.home ? full.home.modules : []
            topMargin: Kirigami.Units.smallSpacing
            bottomMargin: Kirigami.Units.smallSpacing
            leftMargin: Kirigami.Units.smallSpacing
            rightMargin: Kirigami.Units.smallSpacing
            spacing: Kirigami.Units.smallSpacing

            delegate: ModuleRow {
                width: ListView.view.width - ListView.view.leftMargin - ListView.view.rightMargin
                module: modelData
                expanded: full.expandedKey === modelData.key
                chartMetric: full.chartMetric
                onToggled: full.expandedKey = expanded ? "" : modelData.key
                onMetricChosen: metric => full.chartMetric = metric
            }

            PlasmaExtras.PlaceholderMessage {
                anchors.centerIn: parent
                width: parent.width - Kirigami.Units.gridUnit * 4
                visible: list.count === 0
                iconName: root.errorText !== "" ? "network-disconnect" : "temperature-normal"
                text: root.errorText !== "" ? root.errorText : i18n("Loading…")
            }
        }
    }

    footer: PlasmaExtras.PlasmoidHeading {
        position: PlasmaComponents.ToolBar.Footer

        contentItem: PlasmaComponents.Label {
            text: {
                if (root.errorText !== "" && list.count > 0) {
                    return root.errorText;
                }
                if (root.selectedFallback) {
                    return i18n("%1 / %2 not found, panel shows %3",
                                Plasmoid.configuration.home, Plasmoid.configuration.module, root.selected.module);
                }
                return full.home ? root.ageText(full.home.modules[0]) : "";
            }
            color: root.errorText !== "" || root.selectedFallback ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
            opacity: root.errorText !== "" || root.selectedFallback ? 1 : 0.7
            font: Kirigami.Theme.smallFont
            wrapMode: Text.Wrap
        }
    }
}
