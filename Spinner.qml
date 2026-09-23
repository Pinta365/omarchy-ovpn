import QtQuick
import QtQuick.Shapes
import qs.Commons

// A rotating arc, drawn rather than a font glyph.
Item {
  id: root

  property real size: Style.font.icon
  property color color: Color.foreground
  property bool running: true
  property real thickness: Math.max(1.5, size / 9)
  property real sweep: 280

  implicitWidth: size
  implicitHeight: size
  width: implicitWidth
  height: implicitHeight
  // Opacity rather than visible, so the fade renders.
  opacity: running ? 1 : 0

  Behavior on opacity { NumberAnimation { duration: 150 } }

  Shape {
    id: shape
    anchors.fill: parent
    preferredRendererType: Shape.CurveRenderer

    ShapePath {
      strokeColor: root.color
      strokeWidth: root.thickness
      fillColor: "transparent"
      capStyle: ShapePath.RoundCap

      PathAngleArc {
        centerX: root.width / 2
        centerY: root.height / 2
        radiusX: (root.width - root.thickness) / 2
        radiusY: (root.height - root.thickness) / 2
        startAngle: -90
        sweepAngle: root.sweep
      }
    }

    RotationAnimation on rotation {
      from: 0
      to: 360
      duration: 900
      loops: Animation.Infinite
      running: root.running
    }
  }
}
