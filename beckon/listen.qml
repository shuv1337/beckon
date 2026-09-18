// Tiny pulsing dot while a Live session is listening.
// Full-screen click-through overlay (same shape as glow.qml) with a corner
// marker — a tiny PanelWindow can end up zero-sized on this Quickshell.
import Quickshell
import Quickshell.Wayland
import QtQuick

ShellRoot {
  Variants {
    model: Quickshell.screens
    PanelWindow {
      required property var modelData
      screen: modelData
      anchors { top: true; bottom: true; left: true; right: true }
      color: "transparent"
      exclusiveZone: 0
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.namespace: "beckon-listen"
      mask: Region {}          // let every click pass straight through

      Rectangle {
        anchors { right: parent.right; bottom: parent.bottom; rightMargin: 22; bottomMargin: 22 }
        width: 18
        height: 18
        radius: 9
        color: "#ff8a1f"
        border.color: Qt.rgba(1, 1, 1, 0.55)
        border.width: 2
        SequentialAnimation on opacity {
          loops: Animation.Infinite
          NumberAnimation { from: 0.45; to: 1.0; duration: 1100; easing.type: Easing.InOutSine }
          NumberAnimation { from: 1.0; to: 0.45; duration: 1100; easing.type: Easing.InOutSine }
        }
      }
    }
  }
}
