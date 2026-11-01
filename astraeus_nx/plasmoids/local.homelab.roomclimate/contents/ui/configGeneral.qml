import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: page

    property string cfg_prometheusUrl
    property string cfg_home
    property string cfg_module
    property int cfg_refreshInterval
    property int cfg_co2Warning
    property int cfg_co2Critical
    property string cfg_grafanaUrl

    property var modules: []      // [{label, home, module}], modules with a CO2 sensor
    property string loadError: ""

    readonly property var choices: {
        const list = [{ label: i18n("Automatic (base station)"), home: "", module: "" }].concat(modules);
        // Keep a configured module selectable while Prometheus is unreachable.
        if (cfg_module !== "" && !list.some(c => c.home === cfg_home && c.module === cfg_module)) {
            list.push({ label: cfg_home + " / " + cfg_module, home: cfg_home, module: cfg_module });
        }
        return list;
    }

    function loadModules() {
        const base = cfg_prometheusUrl.replace(/\/+$/, "");
        const xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== XMLHttpRequest.DONE) {
                return;
            }
            try {
                if (xhr.status !== 200) {
                    throw new Error(xhr.status === 0 ? i18n("Prometheus not reachable at %1", base)
                                                     : i18n("Prometheus answered HTTP %1", xhr.status));
                }
                const result = JSON.parse(xhr.responseText).data.result;
                modules = result.map(r => ({
                    label: r.metric.home + " / " + r.metric.module,
                    home: r.metric.home,
                    module: r.metric.module
                })).sort((a, b) => a.label.localeCompare(b.label));
                loadError = "";
            } catch (e) {
                modules = [];
                loadError = e.message;
            }
        };
        xhr.open("GET", base + "/api/v1/query?query=" + encodeURIComponent("netatmo_sensor_co2_ppm"));
        xhr.send();
    }

    Component.onCompleted: loadModules()

    Kirigami.FormLayout {
        QQC2.TextField {
            Kirigami.FormData.label: i18n("Prometheus URL:")
            Layout.fillWidth: true
            text: page.cfg_prometheusUrl
            onTextEdited: page.cfg_prometheusUrl = text
            onEditingFinished: page.loadModules()
        }

        QQC2.ComboBox {
            Kirigami.FormData.label: i18n("Shown in the panel:")
            model: page.choices
            textRole: "label"
            currentIndex: page.choices.findIndex(c => c.home === page.cfg_home && c.module === page.cfg_module)
            onActivated: index => {
                page.cfg_home = page.choices[index].home;
                page.cfg_module = page.choices[index].module;
            }
        }

        QQC2.Label {
            visible: page.loadError !== ""
            text: page.loadError
            color: Kirigami.Theme.negativeTextColor
            wrapMode: Text.Wrap
        }

        QQC2.SpinBox {
            Kirigami.FormData.label: i18n("Refresh every (minutes):")
            from: 1
            to: 60
            value: page.cfg_refreshInterval
            onValueModified: page.cfg_refreshInterval = value
        }

        Item {
            Kirigami.FormData.isSection: true
        }

        QQC2.SpinBox {
            Kirigami.FormData.label: i18n("CO₂ warning from (ppm):")
            from: 400
            to: 5000
            stepSize: 100
            value: page.cfg_co2Warning
            onValueModified: page.cfg_co2Warning = value
        }

        QQC2.SpinBox {
            Kirigami.FormData.label: i18n("CO₂ critical from (ppm):")
            from: 400
            to: 5000
            stepSize: 100
            value: page.cfg_co2Critical
            onValueModified: page.cfg_co2Critical = value
        }

        Item {
            Kirigami.FormData.isSection: true
        }

        QQC2.TextField {
            Kirigami.FormData.label: i18n("Grafana dashboard:")
            Layout.fillWidth: true
            placeholderText: i18n("Empty: no button")
            text: page.cfg_grafanaUrl
            onTextEdited: page.cfg_grafanaUrl = text
        }
    }
}
