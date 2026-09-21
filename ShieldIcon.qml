import QtQuick
import QtQuick.Shapes
import qs.Commons

// Drawn rather than a font glyph, which may not paint cleanly at bar size.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground
  property bool filled: false
  property bool pulsing: false
  // Cross instead of check, for the kill switch.
  property bool blocked: false

  implicitWidth: iconSize * 0.86
  implicitHeight: iconSize
  width: implicitWidth
  height: implicitHeight

  readonly property real w: width
  readonly property real h: height
  readonly property real stroke: Math.max(1.2, iconSize / 11)

  SequentialAnimation on opacity {
    running: root.pulsing
    loops: Animation.Infinite
    alwaysRunToEnd: false
    NumberAnimation { to: 0.35; duration: 520; easing.type: Easing.InOutQuad }
    NumberAnimation { to: 1.0; duration: 520; easing.type: Easing.InOutQuad }
    onStopped: root.opacity = 1.0
  }

  Shape {
    anchors.fill: parent
    preferredRendererType: Shape.CurveRenderer

    ShapePath {
      strokeColor: root.color
      strokeWidth: root.stroke
      fillColor: root.filled ? root.color : "transparent"
      joinStyle: ShapePath.RoundJoin
      capStyle: ShapePath.RoundCap

      startX: root.w / 2; startY: root.stroke
      PathLine { x: root.w - root.stroke; y: root.h * 0.18 }
      PathCubic {
        x: root.w / 2; y: root.h - root.stroke
        control1X: root.w - root.stroke; control1Y: root.h * 0.62
        control2X: root.w * 0.78; control2Y: root.h * 0.84
      }
      PathCubic {
        x: root.stroke; y: root.h * 0.18
        control1X: root.w * 0.22; control1Y: root.h * 0.84
        control2X: root.stroke; control2Y: root.h * 0.62
      }
      PathLine { x: root.w / 2; y: root.stroke }
    }

    // Background-coloured stroke reads as cut out of the filled shield.
    ShapePath {
      strokeColor: root.filled ? Color.background : "transparent"
      strokeWidth: root.stroke * 1.1
      fillColor: "transparent"
      capStyle: ShapePath.RoundCap
      joinStyle: ShapePath.RoundJoin

      startX: root.blocked ? root.w * 0.34 : root.w * 0.3
      startY: root.blocked ? root.h * 0.32 : root.h * 0.48
      PathLine {
        x: root.blocked ? root.w * 0.66 : root.w * 0.45
        y: root.blocked ? root.h * 0.62 : root.h * 0.62
      }
      PathMove {
        x: root.blocked ? root.w * 0.66 : root.w * 0.45
        y: root.blocked ? root.h * 0.32 : root.h * 0.62
      }
      PathLine {
        x: root.blocked ? root.w * 0.34 : root.w * 0.71
        y: root.blocked ? root.h * 0.62 : root.h * 0.34
      }
    }
  }
}
